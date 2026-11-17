"""Helpers for the integration tests: the isolated stack and an MQTT observer.

The stack runs under its own Compose project (``monitoring-mqtt-it``), never the one the
host and Dev Container share. Tests see the system the way any client does: through the
broker. What each monitor received comes from the status it publishes itself.
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from monitoring_mqtt.cli import compose
from monitoring_mqtt.core.alerts import ALERT_EVENTS_FILTER, AlertEvent
from monitoring_mqtt.core.registry import WORKER_CLAIMS_FILTER, WorkerClaim
from monitoring_mqtt.core.simulation import SIM_EVENTS_FILTER, SimEvent
from monitoring_mqtt.core.status import MONITOR_STATUS_FILTER, MonitorStatus
from monitoring_mqtt.core.topics import ALL_TELEMETRY_FILTER, SMOKE_DOMAIN

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = Path(__file__).with_name("experiment.yaml")
PROJECT = "monitoring-mqtt-it"
NETWORK = f"{PROJECT}_default"
DEFAULT_TIMEOUT = 30.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Stack:
    """The isolated stack: run the real CLI and Docker against it."""

    env: dict[str, str]
    host: str
    port: int
    api_url: str = ""
    dashboard_url: str = ""

    def api(self, path: str, **params) -> dict | list:
        """GET a JSON document from the API of the test stack."""
        response = httpx.get(f"{self.api_url}{path}", params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def api_status(self, path: str) -> int:
        try:
            return httpx.get(f"{self.api_url}{path}", timeout=5).status_code
        except httpx.HTTPError:
            return 0

    def all_measurements(self, **params) -> list[dict]:
        """Every page of /measurements for these filters."""
        items, cursor = [], None
        while True:
            page = self.api("/api/measurements", limit=1000, **params,
                            **({"cursor": cursor} if cursor else {}))  # fmt: skip
            items += page["items"]  # type: ignore[index]
            cursor = page["next_cursor"]  # type: ignore[index]
            if not cursor:
                return items

    def cli(self, *args: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-m", "monitoring_mqtt.cli.main", "--project-dir", str(ROOT)]
        return subprocess.run(
            [*command, *args], env=self.env, capture_output=True, text=True, timeout=timeout,
            check=False,
        )  # fmt: skip

    def compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = compose.build_command(ROOT, *args)
        # Never the shared stack: always the isolated project, whatever os.environ says.
        argv[argv.index("--project-name") + 1] = PROJECT
        return subprocess.run(argv, env=self.env, capture_output=True, text=True, check=False)

    def start(self, *services: str) -> None:
        """Start stopped containers of ``services``.

        Not ``compose start``: without the experiment's replica variables, Compose would
        skip services whose default is 0 replicas (monitor-all) even though they exist.
        """
        ids = self.compose("ps", "--all", "--quiet", *services).stdout.split()
        assert ids, f"no containers for {services}"
        result = self.docker("start", *ids)
        assert result.returncode == 0, result.stderr

    def docker(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)

    @staticmethod
    def container(service: str, index: int = 1) -> str:
        return f"{PROJECT}-{service}-{index}"


@dataclass
class Sample:
    sequence: int
    value: float
    event_id: str
    received_at: float
    latency: float  # seconds from generation to the observer


@dataclass
class Observer:
    """Subscribes to telemetry, registry, monitor status and ground truth."""

    host: str
    port: int
    telemetry: dict[str, list[Sample]] = field(default_factory=dict)
    statuses: dict[str, MonitorStatus | None] = field(default_factory=dict)
    claims: dict[str, WorkerClaim | None] = field(default_factory=dict)
    sim: list[SimEvent] = field(default_factory=list)
    alerts: list[AlertEvent] = field(default_factory=list)
    connects: int = 0

    def __post_init__(self) -> None:
        self._lock = threading.RLock()  # queries run inside wait_until, which holds it
        self._client = mqtt.Client(
            CallbackAPIVersion.VERSION2, client_id=f"it-observer-{uuid.uuid4().hex[:6]}",
            protocol=mqtt.MQTTv311, clean_session=True,
        )  # fmt: skip
        self._client.reconnect_delay_set(1, 2)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def start(self) -> None:
        self._client.connect_async(self.host, self.port, keepalive=5)
        self._client.loop_start()
        self.wait_until(lambda: self.connects > 0, "observer connected")

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    def publish(self, topic: str, payload: bytes) -> None:
        self._client.publish(topic, payload, qos=1).wait_for_publish(5)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        with self._lock:
            self.connects += 1
        client.subscribe(
            [(ALL_TELEMETRY_FILTER, 1), (WORKER_CLAIMS_FILTER, 1), (MONITOR_STATUS_FILTER, 1),
             (SIM_EVENTS_FILTER, 1), (ALERT_EVENTS_FILTER, 1)]
        )  # fmt: skip

    def _on_message(self, client, userdata, message: mqtt.MQTTMessage) -> None:
        topic, payload, now = message.topic, message.payload, time.time()
        with self._lock:
            if topic.startswith("monitoring/v1/"):
                try:
                    body = json.loads(payload)
                    generated = datetime.fromisoformat(body["timestamp"]).timestamp()
                    sample = Sample(body["sequence"], body["value"], body["event_id"], now,
                                    now - generated)  # fmt: skip
                except (ValueError, KeyError, TypeError):
                    return  # invalid messages published by the tests themselves
                if not topic.endswith(f"/{body['device_id']}"):
                    return  # payload of another device: monitors reject it as invalid
                if body.get("domain") == SMOKE_DOMAIN:
                    return  # `test smoke` probes are not devices
                self.telemetry.setdefault(body["device_id"], []).append(sample)
            elif "/registry/" in topic:
                worker = topic.rsplit("/", 1)[1]
                self.claims[worker] = WorkerClaim.from_payload(payload) if payload else None
            elif "/status/" in topic:
                monitor = topic.rsplit("/", 1)[1]
                self.statuses[monitor] = MonitorStatus.from_payload(payload) if payload else None
            elif "/sim/" in topic:
                self.sim.append(SimEvent.from_payload(payload))
            elif "/alerts/" in topic:
                self.alerts.append(AlertEvent.from_payload(payload))

    # Queries -------------------------------------------------------------------------

    def wait_until(
        self,
        condition: Callable[[], object],
        what: str | Callable[[], str],
        timeout: float = DEFAULT_TIMEOUT,
    ):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                result = condition()
            if result:
                return result
            time.sleep(0.1)
        with self._lock:
            description = what() if callable(what) else what
        raise AssertionError(f"timed out after {timeout:g}s waiting for: {description}")

    def devices(self, domain: str | None = None) -> list[str]:
        with self._lock:
            ids = sorted(self.telemetry)
        return [d for d in ids if domain is None or d.startswith(f"{domain}-")]

    def active(self, domain: str | None = None, within: float = 3.0) -> list[str]:
        """Devices that published in the last ``within`` seconds (not removed ones)."""
        since = time.time() - within
        return [d for d in self.devices(domain) if self.samples(d, since=since)]

    def samples(self, device_id: str, since: float = 0.0) -> list[Sample]:
        with self._lock:
            return [s for s in self.telemetry.get(device_id, []) if s.received_at >= since]

    def count(self, device_id: str) -> int:
        with self._lock:
            return len(self.telemetry.get(device_id, []))

    def monitors(self, prefix: str = "monitor-") -> dict[str, MonitorStatus]:
        with self._lock:
            return {k: v for k, v in self.statuses.items() if v and k.startswith(prefix)}

    def online_monitors(self, kind: str) -> dict[str, MonitorStatus]:
        return {k: v for k, v in self.monitors(f"monitor-{kind}-").items() if v.state == "online"}

    def present_claims(self) -> dict[str, WorkerClaim]:
        with self._lock:
            return {k: v for k, v in self.claims.items() if v is not None}

    def events(self, device_id: str) -> list[SimEvent]:
        with self._lock:
            return [e for e in self.sim if e.device_id == device_id]


def sequences(samples: list[Sample]) -> list[int]:
    return [s.sequence for s in samples]


def gaps(samples: list[Sample]) -> int:
    """Measurements missing between consecutive samples (restarts excluded)."""
    seqs = sequences(samples)
    return sum(b - a - 1 for a, b in itertools.pairwise(seqs) if b > a + 1)


def in_dev_container() -> bool:
    return os.environ.get("DEVCONTAINER") == "true"
