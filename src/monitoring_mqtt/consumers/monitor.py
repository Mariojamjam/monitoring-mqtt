"""Generic telemetry monitor: ``python -m monitoring_mqtt.consumers.monitor``.

Subscribes to ``MQTT_TOPIC_FILTER`` and logs every valid measurement. The same code runs
as different monitors (aquaculture, poultry, global...) just by changing the filter.
Invalid messages are logged and skipped; they never stop the monitor.

The monitor also announces its status on the observability plane (``core/status.py``):
online with its counters every ``STATUS_INTERVAL_SECONDS``, offline through its Last Will,
stopped on a clean shutdown. Publishers never read it.
"""

from __future__ import annotations

import logging
import os
import socket
import statistics
import sys
import threading
from collections import Counter, deque
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.config import ConfigError, MqttSettings, env_str, log_level_from_env
from monitoring_mqtt.core.identity import host_short_id, sanitize_segment
from monitoring_mqtt.core.lifecycle import shutdown_event
from monitoring_mqtt.core.logging import configure_logging, kv
from monitoring_mqtt.core.models import Measurement, TopicMismatchError
from monitoring_mqtt.core.status import (
    RECENT_MESSAGES,
    STATUS_INTERVAL_SECONDS,
    STATUS_QOS,
    LatencyStats,
    MonitorCounters,
    MonitorState,
    MonitorStatus,
    ReceivedMessage,
    node_name,
)
from monitoring_mqtt.core.topics import InvalidTopicError, validate_filter

logger = logging.getLogger("monitoring_mqtt.monitor")

LATENCY_WINDOW = 1_000  # latest samples used for avg/p95
STOP_PUBLISH_TIMEOUT_SECONDS = 2.0


class InvalidMessageError(ValueError):
    """A received message violates the telemetry contract."""


def decode_measurement(topic: str, payload: bytes) -> Measurement:
    """Validate payload and topic consistency, raising ``InvalidMessageError``."""
    try:
        measurement = Measurement.from_payload(payload)
        measurement.ensure_matches_topic(topic)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "payload"
        raise InvalidMessageError(
            f"{exc.error_count()} validation error(s); first: {location}: {first['msg']}"
        ) from exc
    except (TopicMismatchError, InvalidTopicError) as exc:
        raise InvalidMessageError(str(exc)) from exc
    return measurement


class MonitorStats:
    """Counters updated from the network thread and read by the status publisher."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.received = 0
        self.invalid = 0
        self.by_domain: Counter[str] = Counter()
        self.devices: set[str] = set()
        self.latencies: deque[float] = deque(maxlen=LATENCY_WINDOW)
        self.last_received_at: datetime | None = None
        # Kept as plain tuples: the models are only built when a status is published.
        self.recent: deque[tuple[str, Measurement, datetime, float]] = deque(maxlen=RECENT_MESSAGES)

    def record(self, topic: str, measurement: Measurement, latency_ms: float, at: datetime) -> None:
        with self._lock:
            self.received += 1
            self.by_domain[measurement.domain] += 1
            self.devices.add(measurement.device_id)
            self.latencies.append(latency_ms)
            self.last_received_at = at
            self.recent.append((topic, measurement, at, latency_ms))

    def record_invalid(self) -> None:
        with self._lock:
            self.invalid += 1

    def snapshot(self) -> MonitorCounters:
        with self._lock:
            latency = None
            if self.latencies:
                samples = sorted(self.latencies)
                latency = LatencyStats(
                    last=round(self.latencies[-1], 3),
                    avg=round(statistics.fmean(samples), 3),
                    p95=round(samples[min(len(samples) - 1, int(0.95 * len(samples)))], 3),
                )
            return MonitorCounters(
                received=self.received,
                invalid=self.invalid,
                devices_seen=len(self.devices),
                by_domain=dict(self.by_domain),
                latency_ms=latency,
                last_received_at=self.last_received_at,
                recent=[
                    ReceivedMessage(
                        topic=topic,
                        device_id=m.device_id,
                        value=m.value,
                        unit=m.unit,
                        sequence=m.sequence,
                        event_id=m.event_id,
                        received_at=at,
                        latency_ms=round(latency, 3),
                    )
                    for topic, m, at, latency in self.recent
                ],
            )


class Monitor:
    def __init__(
        self,
        name: str,
        topic_filter: str,
        settings: MqttSettings,
        node: str | None = None,
        hostname: str | None = None,
    ) -> None:
        self.name = name
        self.topic_filter = validate_filter(topic_filter)
        self.settings = settings
        self.node = node
        self.hostname = hostname or socket.gethostname()
        self.stats = MonitorStats()
        self.client_id = f"monitor-{name}-{host_short_id(self.hostname)}"
        self.started_at = datetime.now(UTC)
        self.client = mqtt_helpers.create_client(
            self.client_id, settings, on_connected=self._on_connected
        )
        self.client.on_message = self._on_message
        # Set before connecting: the broker announces "offline" if the connection is lost.
        offline = self.status(MonitorState.OFFLINE, counters=False)
        self.client.will_set(offline.topic, offline.to_payload(), qos=STATUS_QOS, retain=True)

    def status(self, state: MonitorState, counters: bool = True) -> MonitorStatus:
        return MonitorStatus(
            client_id=self.client_id,
            monitor=self.name,
            topic_filter=self.topic_filter,
            node=self.node,
            hostname=self.hostname,
            state=state,
            started_at=self.started_at,
            updated_at=datetime.now(UTC),
            counters=self.stats.snapshot() if counters else None,
        )

    def publish_status(self, state: MonitorState = MonitorState.ONLINE, retain: bool = True):
        status = self.status(state)
        return self.client.publish(status.topic, status.to_payload(), qos=STATUS_QOS, retain=retain)

    def _on_connected(self, client: mqtt.Client) -> None:
        # Clean session: subscriptions must be renewed after every reconnection.
        client.subscribe(self.topic_filter, qos=self.settings.qos)
        logger.info("subscribed %s", kv(filter=self.topic_filter, qos=self.settings.qos))
        self.publish_status()

    def _on_message(self, client: mqtt.Client, userdata: object, message: mqtt.MQTTMessage) -> None:
        # Exceptions escaping a Paho callback would kill the network thread.
        try:
            self.handle(message.topic, message.payload)
        except Exception:
            logger.exception("unexpected error handling message %s", kv(topic=message.topic))

    def handle(self, topic: str, payload: bytes) -> Measurement | None:
        try:
            measurement = decode_measurement(topic, payload)
        except InvalidMessageError as exc:
            self.stats.record_invalid()
            logger.warning("invalid message %s error=%s", kv(topic=topic), exc)
            return None

        now = datetime.now(UTC)
        latency_ms = (now - measurement.timestamp).total_seconds() * 1000
        self.stats.record(topic, measurement, latency_ms, now)
        logger.info(
            "received %s",
            kv(
                topic=topic,
                device_id=measurement.device_id,
                seq=measurement.sequence,
                value=measurement.value,
                unit=measurement.unit,
                event_id=measurement.event_id,
                latency_ms=f"{latency_ms:.1f}",
            ),
        )
        return measurement

    def shutdown(self) -> None:
        """Announce the clean stop, remove the retained status and disconnect."""
        if self.client.is_connected():
            try:
                self.publish_status(MonitorState.STOPPED, retain=False)
                cleared = self.client.publish(
                    self.status(MonitorState.STOPPED).topic, None, qos=STATUS_QOS, retain=True
                )
                cleared.wait_for_publish(STOP_PUBLISH_TIMEOUT_SECONDS)
            except (RuntimeError, ValueError):
                logger.warning("status not cleared %s", kv(client_id=self.client_id))
        mqtt_helpers.stop(self.client)

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or shutdown_event()
        mqtt_helpers.start(self.client, self.settings)
        try:
            while not stop.wait(STATUS_INTERVAL_SECONDS):
                if self.client.is_connected():
                    self.publish_status()
        finally:
            self.shutdown()
            logger.info(
                "monitor stopped %s",
                kv(received=self.stats.received, invalid=self.stats.invalid),
            )


def main() -> int:
    try:
        configure_logging(log_level_from_env())
        settings = MqttSettings.from_env()
        topic_filter = validate_filter(env_str(os.environ, "MQTT_TOPIC_FILTER"))
        name = sanitize_segment(env_str(os.environ, "MONITOR_NAME", "monitor"))
    except (ConfigError, InvalidTopicError, ValueError) as exc:
        logging.basicConfig()
        logger.error("invalid configuration: %s", exc)
        return 2

    monitor = Monitor(name, topic_filter, settings, node=node_name())
    logger.info(
        "monitor starting %s",
        kv(
            name=name,
            client_id=monitor.client_id,
            filter=topic_filter,
            broker=f"{settings.host}:{settings.port}",
        ),
    )
    monitor.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
