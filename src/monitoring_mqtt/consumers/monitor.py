"""Generic telemetry monitor: ``python -m monitoring_mqtt.consumers.monitor``.

Subscribes to ``MQTT_TOPIC_FILTER`` and logs every valid measurement. The same code runs
as different monitors (aquaculture, poultry, global...) just by changing the filter.
Invalid messages are logged and skipped; they never stop the monitor.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.config import ConfigError, MqttSettings, env_str, log_level_from_env
from monitoring_mqtt.core.identity import host_short_id, sanitize_segment
from monitoring_mqtt.core.lifecycle import shutdown_event
from monitoring_mqtt.core.logging import configure_logging, kv
from monitoring_mqtt.core.models import Measurement, TopicMismatchError
from monitoring_mqtt.core.topics import InvalidTopicError, validate_filter

logger = logging.getLogger("monitoring_mqtt.monitor")


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


@dataclass
class MonitorStats:
    received: int = 0
    invalid: int = 0


class Monitor:
    def __init__(self, name: str, topic_filter: str, settings: MqttSettings) -> None:
        self.name = name
        self.topic_filter = validate_filter(topic_filter)
        self.settings = settings
        self.stats = MonitorStats()
        self.client_id = f"monitor-{name}-{host_short_id()}"
        self.client = mqtt_helpers.create_client(
            self.client_id, settings, on_connected=self._subscribe
        )
        self.client.on_message = self._on_message

    def _subscribe(self, client: mqtt.Client) -> None:
        # Clean session: subscriptions must be renewed after every reconnection.
        client.subscribe(self.topic_filter, qos=self.settings.qos)
        logger.info("subscribed %s", kv(filter=self.topic_filter, qos=self.settings.qos))

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
            self.stats.invalid += 1
            logger.warning("invalid message %s error=%s", kv(topic=topic), exc)
            return None

        self.stats.received += 1
        latency_ms = (datetime.now(UTC) - measurement.timestamp).total_seconds() * 1000
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

    def run(self) -> None:
        stop = shutdown_event()
        mqtt_helpers.start(self.client, self.settings)
        try:
            stop.wait()
        finally:
            mqtt_helpers.stop(self.client)
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

    monitor = Monitor(name, topic_filter, settings)
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
