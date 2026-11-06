"""End-to-end smoke test: ``python -m monitoring_mqtt.testing.smoke``.

Runs inside the Compose network (``monitoring-mqtt test smoke``) and checks two things:

1. Probe round trip: an independent publisher sends a probe event with a known ``event_id``
   on ``monitoring/v1/smoke/probe/<id>``; an independent subscriber must receive it through
   the broker within the timeout. No monitor subscribes to the ``smoke`` domain, so the
   probe does not disturb the running stack.
2. Live telemetry: at least one valid measurement from the running devices must arrive
   within the telemetry window.

Exit code 0 when both pass, 1 otherwise, 2 on invalid configuration.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt

from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.config import (
    ConfigError,
    MqttSettings,
    env_float,
    log_level_from_env,
)
from monitoring_mqtt.core.logging import configure_logging
from monitoring_mqtt.core.models import Measurement, TopicMismatchError
from monitoring_mqtt.core.topics import ALL_TELEMETRY_FILTER, InvalidTopicError

SMOKE_DOMAIN = "smoke"
SMOKE_METRIC = "probe"

logger = logging.getLogger("monitoring_mqtt.smoke")


def build_probe(suffix: str) -> Measurement:
    return Measurement.create(
        run_id=f"smoke-{suffix}",
        device_id=f"smoke-{suffix}",
        device_type="smoke_probe",
        domain=SMOKE_DOMAIN,
        site_id="smoke",
        metric=SMOKE_METRIC,
        value=1.0,
        unit=None,
        sequence=1,
    )


@dataclass
class Observation:
    """What the smoke subscriber saw. Thread-safe updates via ``record``."""

    probe_event_id: uuid.UUID
    probe_received: threading.Event = field(default_factory=threading.Event)
    telemetry_received: threading.Event = field(default_factory=threading.Event)
    devices: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    telemetry_messages: int = 0
    invalid_messages: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, topic: str, payload: bytes) -> None:
        try:
            measurement = Measurement.from_payload(payload)
            measurement.ensure_matches_topic(topic)
        except (ValueError, TopicMismatchError, InvalidTopicError):
            with self._lock:
                self.invalid_messages += 1
            return

        if measurement.domain == SMOKE_DOMAIN:
            if measurement.event_id == self.probe_event_id:
                self.probe_received.set()
            return
        with self._lock:
            self.telemetry_messages += 1
            self.devices.add(measurement.device_id)
            self.domains.add(measurement.domain)
        self.telemetry_received.set()


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def report(checks: list[Check]) -> int:
    for check in checks:
        if check.passed:
            logger.info("[ok] %s: %s", check.name, check.detail)
        else:
            logger.error("[FAIL] %s: %s", check.name, check.detail)
    passed = all(check.passed for check in checks)
    (logger.info if passed else logger.error)("SMOKE TEST %s", "PASSED" if passed else "FAILED")
    return 0 if passed else 1


def _wait(event: threading.Event, deadline: float) -> bool:
    return event.wait(max(0.0, deadline - time.monotonic()))


def run_smoke(settings: MqttSettings, timeout: float, telemetry_window: float) -> int:
    suffix = uuid.uuid4().hex[:8]
    probe = build_probe(suffix)
    observation = Observation(probe.event_id)
    subscribed = threading.Event()
    broker = f"{settings.host}:{settings.port}"
    start = time.monotonic()

    def on_connected(client: mqtt.Client) -> None:
        client.subscribe(ALL_TELEMETRY_FILTER, qos=settings.qos)

    def on_subscribe(client, userdata, mid, reason_codes, properties) -> None:
        if not any(code.is_failure for code in reason_codes):
            subscribed.set()

    def on_message(client, userdata, message: mqtt.MQTTMessage) -> None:
        try:
            observation.record(message.topic, message.payload)
        except Exception:
            logger.exception("unexpected error handling %s", message.topic)

    subscriber = mqtt_helpers.create_client(
        f"smoke-sub-{suffix}", settings, on_connected=on_connected
    )
    subscriber.on_subscribe = on_subscribe
    subscriber.on_message = on_message
    publisher_connected = threading.Event()
    publisher = mqtt_helpers.create_client(
        f"smoke-pub-{suffix}", settings, on_connected=lambda client: publisher_connected.set()
    )

    logger.info("smoke test starting broker=%s timeout_s=%s", broker, timeout)
    checks: list[Check] = []
    mqtt_helpers.start(subscriber, settings)
    mqtt_helpers.start(publisher, settings)
    try:
        deadline = start + timeout
        if not _wait(subscribed, deadline) or not _wait(publisher_connected, deadline):
            checks.append(
                Check("broker", False, f"could not connect/subscribe to {broker} in {timeout}s")
            )
            return report(checks)
        checks.append(Check("broker", True, f"connected and subscribed to {broker}"))

        sent_at = time.monotonic()
        info = publisher.publish(probe.topic, probe.to_payload(), qos=settings.qos, retain=False)
        info.wait_for_publish(max(0.1, deadline - time.monotonic()))
        acked = info.is_published()
        received = _wait(observation.probe_received, deadline)
        elapsed_ms = (time.monotonic() - sent_at) * 1000
        checks.append(
            Check(
                "probe round trip",
                acked and received,
                f"event_id={probe.event_id} topic={probe.topic} puback={acked} "
                f"received={received}" + (f" in {elapsed_ms:.1f} ms" if received else ""),
            )
        )

        telemetry = _wait(observation.telemetry_received, start + telemetry_window)
        checks.append(
            Check(
                "live telemetry",
                telemetry,
                f"devices are publishing; first {observation.telemetry_messages} valid "
                f"message(s) from {len(observation.devices)} device(s), domains="
                f"{','.join(sorted(observation.domains)) or '-'}, "
                f"invalid={observation.invalid_messages}"
                if telemetry
                else f"no device telemetry within {telemetry_window}s (is the stack up?)",
            )
        )
        return report(checks)
    finally:
        mqtt_helpers.stop(publisher)
        mqtt_helpers.stop(subscriber)


def main() -> int:
    try:
        configure_logging(log_level_from_env())
        settings = MqttSettings.from_env()
        timeout = env_float(os.environ, "SMOKE_TIMEOUT_SECONDS", 10.0, minimum=1.0)
        window = env_float(os.environ, "SMOKE_TELEMETRY_WINDOW_SECONDS", 15.0, minimum=1.0)
    except ConfigError as exc:
        logging.basicConfig()
        logger.error("invalid configuration: %s", exc)
        return 2
    return run_smoke(settings, timeout, max(window, timeout))


if __name__ == "__main__":
    sys.exit(main())
