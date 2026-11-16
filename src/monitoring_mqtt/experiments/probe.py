"""Experiment probe: ``python -m monitoring_mqtt.experiments.probe`` (one-off container).

It measures one run from the outside, like any observer, through the broker and the API:

1. waits until every device of the run reports (worker status, ``core/status.py``);
2. waits ``PROBE_WARMUP_SECONDS``, then opens the measured window: it snapshots the
   worker and monitor counters and samples the broker statistics (``$SYS``) every second;
   with ``PROBE_OBSERVE_TELEMETRY`` it also records when each device's messages arrive
   (recovery after failures);
3. after ``PROBE_DURATION_SECONDS`` it closes the window with a second snapshot, waits until
   the ingestion consumer has stored everything (buffer empty, count stable), and asks the
   API what was stored for the window (``GET /api/runs/{id}/window``).

It writes JSON lines to stdout (``ready``, ``window_start``, ``window_end``, ``result`` or
``error``) for the CLI, which runs the failure actions on the same clock. Logs go to stderr.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import paho.mqtt.client as mqtt
from pydantic import ValidationError

from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.config import (
    ConfigError,
    MqttSettings,
    env_bool,
    env_float,
    env_int,
    env_str,
    log_level_from_env,
)
from monitoring_mqtt.core.logging import configure_logging, kv
from monitoring_mqtt.core.status import (
    MONITOR_STATUS_FILTER,
    STATUS_INTERVAL_SECONDS,
    WORKER_STATUS_FILTER,
    MonitorState,
    MonitorStatus,
    WorkerStatus,
)
from monitoring_mqtt.core.topics import ALL_TELEMETRY_FILTER

logger = logging.getLogger("monitoring_mqtt.experiments.probe")

BROKER_STATS_FILTER = "$SYS/broker/#"
# Cumulative broker counters (and the connected clients) sampled during the window.
BROKER_STATS = {
    "$SYS/broker/messages/received": "messages_received",
    "$SYS/broker/messages/sent": "messages_sent",
    "$SYS/broker/bytes/received": "bytes_received",
    "$SYS/broker/bytes/sent": "bytes_sent",
    "$SYS/broker/publish/messages/dropped": "publish_dropped",
    "$SYS/broker/clients/connected": "clients_connected",
}
INGESTION_CLIENT_ID = "ingestion"
SAMPLE_SECONDS = 1.0
POLL_SECONDS = 0.5


def utc_now() -> datetime:
    return datetime.now(UTC)


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, default=str), flush=True)


class Observations:
    """What the probe saw on the broker; updated by the network thread."""

    def __init__(self, run_id: str, observe_telemetry: bool) -> None:
        self.run_id = run_id
        self.observe_telemetry = observe_telemetry
        self._lock = threading.Lock()
        self.workers: dict[str, WorkerStatus] = {}
        self.monitors: dict[str, MonitorStatus] = {}
        # Every status received: counters are interpolated at the window's exact edges.
        self.worker_history: dict[str, list[WorkerStatus]] = {}
        self.monitor_history: dict[str, list[MonitorStatus]] = {}
        self.broker: dict[str, float] = {}
        self.arrivals: dict[str, list[float]] = {}
        self.recording = False

    def handle(self, topic: str, payload: bytes) -> None:
        if topic.startswith("$SYS/"):
            name = BROKER_STATS.get(topic)
            if name is not None:
                try:
                    value = float(payload.decode().split()[0])
                except (ValueError, IndexError, UnicodeDecodeError):
                    return
                with self._lock:
                    self.broker[name] = value
            return
        if "/status/v1/workers/" in topic:
            if not payload:
                return
            try:
                status = WorkerStatus.from_payload(payload)
            except ValidationError:
                return
            if status.run_id == self.run_id:
                with self._lock:
                    self.workers[status.worker_id] = status
                    if status.devices:
                        self.worker_history.setdefault(status.worker_id, []).append(status)
            return
        if "/status/v1/monitors/" in topic:
            if not payload:
                return
            try:
                monitor = MonitorStatus.from_payload(payload)
            except ValidationError:
                return
            with self._lock:
                self.monitors[monitor.client_id] = monitor
                if monitor.counters is not None:
                    self.monitor_history.setdefault(monitor.client_id, []).append(monitor)
            return
        if self.observe_telemetry and self.recording:
            device_id = topic.rsplit("/", 1)[-1]
            with self._lock:
                self.arrivals.setdefault(device_id, []).append(time.time())

    def online_devices(self) -> int:
        with self._lock:
            return sum(
                sum(1 for d in w.devices if d.generated > 0)
                for w in self.workers.values()
                if w.state is MonitorState.ONLINE
            )

    def snapshot(self) -> dict[str, Any]:
        """The counters now: per device (workers) and per subscriber (monitors)."""
        with self._lock:
            workers = {
                w.worker_id: {
                    "updated_at": w.updated_at.isoformat(),
                    "state": w.state.value,
                    "qos": w.qos,
                    "devices": {
                        d.device_id: d.model_dump(exclude={"device_id"}) for d in w.devices
                    },
                }
                for w in self.workers.values()
            }
            monitors = {}
            for m in self.monitors.values():
                if m.counters is None:
                    continue
                monitors[m.client_id] = {
                    "monitor": m.monitor,
                    "filter": m.topic_filter,
                    "state": m.state.value,
                    "updated_at": m.updated_at.isoformat(),
                    "received": m.counters.received,
                    "duplicates": m.counters.duplicates,
                    "invalid": m.counters.invalid,
                    "by_domain": dict(m.counters.by_domain),
                    "delivery": m.delivery.model_dump(mode="json") if m.delivery else None,
                }
            return {
                "at": utc_now().isoformat(),
                "workers": workers,
                "monitors": monitors,
                "broker": dict(self.broker),
            }

    def statuses_after(self, at: datetime) -> bool:
        """Every worker of the run has reported after ``at`` (needed to interpolate)."""
        with self._lock:
            return bool(self.worker_history) and all(
                h[-1].updated_at > at for h in self.worker_history.values()
            )

    def snapshot_at(self, at: datetime) -> dict[str, Any]:
        """The counters at an exact time, interpolated linearly between the statuses
        received just before and just after it (statuses come every few seconds)."""
        with self._lock:
            workers = {}
            for worker_id, history in self.worker_history.items():
                counters = _interpolate(
                    history, at, lambda w: {d.device_id: d.model_dump(exclude={"device_id"})
                                            for d in w.devices})  # fmt: skip
                if counters is not None:
                    workers[worker_id] = {"devices": counters}
            monitors = {}
            for client_id, history in self.monitor_history.items():
                counters = _interpolate(history, at, _monitor_counters)
                if counters is None:
                    continue
                latest = history[-1]
                monitors[client_id] = {
                    "monitor": latest.monitor,
                    "filter": latest.topic_filter,
                    **counters,
                }
            return {"at": at.isoformat(), "workers": workers, "monitors": monitors}

    def broker_sample(self) -> dict[str, float]:
        with self._lock:
            return {"t": time.time(), **self.broker}

    def ingestion(self) -> MonitorStatus | None:
        with self._lock:
            return self.monitors.get(INGESTION_CLIENT_ID)

    def take_arrivals(self) -> dict[str, list[float]]:
        with self._lock:
            return {k: [round(t, 3) for t in v] for k, v in self.arrivals.items()}


def _monitor_counters(m: MonitorStatus) -> dict[str, Any]:
    c = m.counters
    assert c is not None
    delivery = m.delivery.model_dump(mode="json") if m.delivery else None
    numbers = {"received": c.received, "duplicates": c.duplicates, "invalid": c.invalid}
    return {**numbers, "by_domain": dict(c.by_domain), "delivery": delivery}


def _mix(before: Any, after: Any, fraction: float) -> Any:
    """Linear interpolation of nested counters; a counter that went down (the process
    restarted in between) keeps the earlier value."""
    if isinstance(before, dict) and isinstance(after, dict):
        return {k: _mix(before[k], after[k], fraction) if k in after else before[k]
                for k in before}  # fmt: skip
    if isinstance(before, int | float) and isinstance(after, int | float) and after >= before:
        return round(before + fraction * (after - before), 3)
    return before


def _interpolate(history: list, at: datetime, counters) -> dict[str, Any] | None:
    before = [s for s in history if s.updated_at <= at]
    after = [s for s in history if s.updated_at > at]
    if before and after:
        a, b = before[-1], after[0]
        span = (b.updated_at - a.updated_at).total_seconds()
        fraction = (at - a.updated_at).total_seconds() / span if span > 0 else 0.0
        return _mix(counters(a), counters(b), fraction)
    if before:
        return counters(before[-1])
    if after:
        first = after[0]
        if first.started_at > at:  # the process started after `at`: nothing counted yet
            if isinstance(first, WorkerStatus):
                return {d: dict.fromkeys(c, 0) for d, c in counters(first).items()}
            return None
        return counters(first)
    return None


def wait_for(condition, timeout: float, stop: threading.Event | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        if stop is not None and stop.is_set():
            return False
        time.sleep(POLL_SECONDS)
    return condition()


def settle(obs: Observations, window_end: datetime, timeout: float) -> bool:
    """Wait until the ingestion consumer has stored what it received: its status, refreshed
    after the window, shows an empty buffer and a stored count that stopped growing."""
    stable: list[int] = []

    def drained() -> bool:
        status = obs.ingestion()
        if status is None or status.delivery is None or status.updated_at <= window_end:
            return False
        stored = status.delivery.stored
        if status.delivery.buffered == 0 and (not stable or stable[-1] != stored):
            stable.append(stored)
        return (
            len(stable) >= 1
            and status.delivery.buffered == 0
            and (len(stable) >= 2 or (utc_now() - window_end).total_seconds() > 6)
        )

    return wait_for(drained, timeout)


def query_window(api_url: str, run_id: str, since: datetime, until: datetime) -> dict | None:
    for attempt in range(5):
        try:
            response = httpx.get(
                f"{api_url.rstrip('/')}/api/runs/{run_id}/window",
                params={"from": since.isoformat(), "to": until.isoformat()},
                timeout=30,
            )
            if response.status_code == 200:
                return response.json()
            logger.warning("window query %s", kv(status=response.status_code, attempt=attempt))
        except httpx.HTTPError as exc:
            logger.warning("window query failed %s", kv(error=exc, attempt=attempt))
        time.sleep(2)
    return None


def main() -> int:
    env = os.environ
    try:
        configure_logging(log_level_from_env(), stream=sys.stderr)
        settings = replace(MqttSettings.from_env(), clean_session=True)
        run_id = env_str(env, "PROBE_RUN_ID", "")
        if not run_id:
            raise ConfigError("PROBE_RUN_ID is required")
        expected = env_int(env, "PROBE_EXPECTED_DEVICES", 1, minimum=1, maximum=100_000)
        warmup = env_float(env, "PROBE_WARMUP_SECONDS", 30.0, minimum=0.0)
        duration = env_float(env, "PROBE_DURATION_SECONDS", 120.0, minimum=1.0)
        ready_timeout = env_float(env, "PROBE_READY_TIMEOUT_SECONDS", 180.0, minimum=1.0)
        settle_timeout = env_float(env, "PROBE_SETTLE_TIMEOUT_SECONDS", 120.0, minimum=1.0)
        observe = env_bool(env, "PROBE_OBSERVE_TELEMETRY", False)
        api_url = env_str(env, "API_URL", "")
    except ConfigError as exc:
        logging.basicConfig(stream=sys.stderr)
        logger.error("invalid configuration: %s", exc)
        emit("error", reason=str(exc))
        return 2

    obs = Observations(run_id, observe)

    def on_connected(client: mqtt.Client) -> None:
        topics = [WORKER_STATUS_FILTER, MONITOR_STATUS_FILTER, BROKER_STATS_FILTER]
        if observe:
            topics.append(ALL_TELEMETRY_FILTER)
        client.subscribe([(t, 1) for t in topics])

    client = mqtt_helpers.create_client(f"probe-{uuid.uuid4().hex[:8]}", settings, on_connected)
    client.on_message = lambda c, u, m: obs.handle(m.topic, m.payload)
    mqtt_helpers.start(client, settings)
    try:
        if not wait_for(lambda: obs.online_devices() >= expected, ready_timeout):
            emit(
                "error",
                reason=f"only {obs.online_devices()} of {expected} devices reported "
                f"for run {run_id} within {ready_timeout:g}s",
            )
            return 1
        emit("ready", devices=obs.online_devices())
        time.sleep(warmup)

        since = utc_now()
        obs.recording = True
        emit("window_start", at=since.isoformat())
        samples = []
        deadline = time.monotonic() + duration
        while (left := deadline - time.monotonic()) > 0:
            samples.append(obs.broker_sample())
            time.sleep(min(SAMPLE_SECONDS, left))
        until = utc_now()
        obs.recording = False
        samples.append(obs.broker_sample())
        emit("window_end", at=until.isoformat())

        settled = settle(obs, until, settle_timeout) if obs.ingestion() is not None else False
        wait_for(lambda: obs.statuses_after(until), STATUS_INTERVAL_SECONDS * 3)
        start, end = obs.snapshot_at(since), obs.snapshot_at(until)
        window = query_window(api_url, run_id, since, until) if api_url else None
        emit(
            "result",
            run_id=run_id,
            since=since.isoformat(),
            until=until.isoformat(),
            start=start,
            end=end,
            settled=settled,
            after=obs.snapshot(),
            broker=samples,
            window=window,
            arrivals=obs.take_arrivals() if observe else None,
        )
        return 0
    finally:
        mqtt_helpers.stop(client)


if __name__ == "__main__":
    sys.exit(main())
