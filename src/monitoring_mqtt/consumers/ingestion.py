"""Ingestion consumer: ``python -m monitoring_mqtt.consumers.ingestion``.

The bridge between the broker and the API. It subscribes to every measurement and alert
event, validates them with the same contract as the monitors and posts them to the API in
batches. It never opens a database connection: the API owns the database.

Reliability, hop by hop:

- broker → ingestion: a fixed client id (``ingestion``) and a **persistent session**, so
  the broker queues QoS 1 messages while the consumer is down (up to the broker's queue
  limit) and delivers them when it returns. Only one instance may run with this id;
- ingestion → API: a bounded in-memory buffer; batches are retried with backoff while the
  API or its database is down. The API stores each event once (idempotent by id), so a
  retried batch never creates duplicates.

What is buffered in memory is lost if the process dies (the broker already got its
acknowledgement); the stored history then shows a sequence gap.

The consumer reports itself on the observability plane like a monitor, with its delivery
counters, so observers see where the events went.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from monitoring_mqtt.consumers.buffer import BatchBuffer
from monitoring_mqtt.consumers.delivery import (
    ALERTS_PATH,
    MEASUREMENTS_PATH,
    Backoff,
    BatchRejectedError,
    Delivery,
    RetryLaterError,
)
from monitoring_mqtt.consumers.monitor import InvalidMessageError, MonitorStats, decode_measurement
from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.alerts import ALERT_EVENTS_FILTER, ALERTS_PREFIX, AlertEvent
from monitoring_mqtt.core.config import (
    ConfigError,
    MqttSettings,
    env_float,
    env_int,
    env_str,
    log_level_from_env,
)
from monitoring_mqtt.core.ingest import MAX_BATCH_ITEMS, IngestBatch
from monitoring_mqtt.core.lifecycle import shutdown_event
from monitoring_mqtt.core.logging import configure_logging, kv
from monitoring_mqtt.core.status import (
    STATUS_INTERVAL_SECONDS,
    STATUS_QOS,
    DeliveryCounters,
    MonitorState,
    MonitorStatus,
    node_name,
)
from monitoring_mqtt.core.topics import ALL_TELEMETRY_FILTER, SMOKE_DOMAIN

logger = logging.getLogger("monitoring_mqtt.ingestion")

CLIENT_ID = "ingestion"  # fixed: the broker keeps this session while the consumer is down
SHUTDOWN_FLUSH_SECONDS = 5.0
IDLE_POLL_SECONDS = 0.05

Item = tuple[datetime, dict]


@dataclass(frozen=True)
class IngestionSettings:
    api_url: str = "http://api:8000"
    batch_size: int = 500
    flush_interval: float = 1.0
    buffer_limit: int = 50_000

    @classmethod
    def from_env(cls) -> IngestionSettings:
        env = os.environ
        return cls(
            api_url=env_str(env, "API_URL", cls.api_url),
            batch_size=env_int(env, "INGEST_BATCH_SIZE", cls.batch_size, 1, MAX_BATCH_ITEMS),
            flush_interval=env_float(
                env, "INGEST_FLUSH_INTERVAL_SECONDS", cls.flush_interval, minimum=0.05
            ),
            buffer_limit=env_int(env, "INGEST_BUFFER_LIMIT", cls.buffer_limit, 1, 10_000_000),
        )


class DeliveryStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.batches = self.stored = self.duplicates = self.rejected = self.api_errors = 0
        self.last_delivery_at: datetime | None = None

    def delivered(self, stored: int, duplicates: int, rejected: int) -> None:
        with self._lock:
            self.batches += 1
            self.stored += stored
            self.duplicates += duplicates
            self.rejected += rejected
            self.last_delivery_at = datetime.now(UTC)

    def failed(self) -> None:
        with self._lock:
            self.api_errors += 1


class Ingestion:
    def __init__(
        self,
        settings: IngestionSettings,
        mqtt_settings: MqttSettings,
        delivery: Delivery | None = None,
        node: str | None = None,
        hostname: str | None = None,
        client: mqtt.Client | None = None,
    ) -> None:
        self.settings = settings
        # Persistent session whatever the experiment says: it is what keeps the history
        # complete across restarts of this consumer.
        self.mqtt_settings = replace(mqtt_settings, clean_session=False)
        self.delivery = delivery or Delivery(settings.api_url)
        self.node = node
        self.hostname = hostname or socket.gethostname()
        self.started_at = datetime.now(UTC)
        self.stats = MonitorStats()
        self.delivered = DeliveryStats()
        self.measurements: BatchBuffer[Item] = BatchBuffer(settings.buffer_limit)
        self.alerts: BatchBuffer[Item] = BatchBuffer(settings.buffer_limit)
        self.client = client or mqtt_helpers.create_client(
            CLIENT_ID, self.mqtt_settings, on_connected=self._on_connected
        )
        self.client.on_message = self._on_message
        offline = self.status(MonitorState.OFFLINE, counters=False)
        self.client.will_set(offline.topic, offline.to_payload(), qos=STATUS_QOS, retain=True)

    # Status --------------------------------------------------------------------------

    def status(self, state: MonitorState, counters: bool = True) -> MonitorStatus:
        delivery = None
        if counters:
            m, a = self.measurements.stats(), self.alerts.stats()
            d = self.delivered
            delivery = DeliveryCounters(
                api_url=self.settings.api_url, buffered=m.buffered + a.buffered,
                dropped=m.dropped + a.dropped, batches=d.batches, stored=d.stored,
                duplicates=d.duplicates, rejected=d.rejected, api_errors=d.api_errors,
                last_delivery_at=d.last_delivery_at,
            )  # fmt: skip
        return MonitorStatus(
            client_id=CLIENT_ID,
            monitor="ingestion",
            topic_filter=ALL_TELEMETRY_FILTER,
            node=self.node,
            hostname=self.hostname,
            state=state,
            started_at=self.started_at,
            updated_at=datetime.now(UTC),
            counters=self.stats.snapshot() if counters else None,
            delivery=delivery,
        )

    def publish_status(self, state: MonitorState = MonitorState.ONLINE, retain: bool = True):
        status = self.status(state)
        return self.client.publish(status.topic, status.to_payload(), qos=STATUS_QOS, retain=retain)

    # Network thread ------------------------------------------------------------------

    def _on_connected(self, client: mqtt.Client) -> None:
        client.subscribe([(ALL_TELEMETRY_FILTER, 1), (ALERT_EVENTS_FILTER, 1)])
        logger.info("subscribed %s", kv(filters=f"{ALL_TELEMETRY_FILTER},{ALERT_EVENTS_FILTER}"))
        self.publish_status()

    def _on_message(self, client: mqtt.Client, userdata: object, message: mqtt.MQTTMessage) -> None:
        try:
            self.handle(message.topic, message.payload)
        except Exception:
            logger.exception("unexpected error handling message %s", kv(topic=message.topic))

    def handle(self, topic: str, payload: bytes) -> None:
        now = datetime.now(UTC)
        if topic.startswith(f"{ALERTS_PREFIX}/"):
            try:
                alert = AlertEvent.from_payload(payload)
            except ValidationError as exc:
                logger.warning("invalid alert event %s error=%s", kv(topic=topic),
                               exc.errors()[0]["msg"])  # fmt: skip
                return
            self.alerts.add((now, alert.model_dump(mode="json")))
            return
        try:
            measurement = decode_measurement(topic, payload)
        except InvalidMessageError as exc:
            self.stats.record_invalid()
            logger.warning("invalid message %s error=%s", kv(topic=topic), exc)
            return
        if measurement.domain == SMOKE_DOMAIN:
            return  # `test smoke` probes are not part of the history
        latency_ms = (now - measurement.timestamp).total_seconds() * 1000
        self.stats.record(topic, measurement, latency_ms, now)
        self.measurements.add((now, measurement.model_dump(mode="json")))

    # Delivery thread -----------------------------------------------------------------

    def deliver_once(self, buffer: BatchBuffer[Item], path: str, force: bool = False) -> bool:
        """Send one batch if one is due; return False when the API must be retried later."""
        size = self.settings.batch_size
        if not force and not buffer.ready(size, self.settings.flush_interval):
            return True
        batch = buffer.take(size)
        if not batch:
            return True
        body = IngestBatch.of(CLIENT_ID, batch)
        try:
            result = self.delivery.send(path, body)
        except RetryLaterError as exc:
            buffer.give_back(batch)
            self.delivered.failed()
            fields = kv(path=path, error=exc, buffered=len(buffer))
            logger.warning("API unavailable, will retry %s", fields)
            return False
        except BatchRejectedError as exc:
            self.delivered.failed()
            fields = kv(path=path, items=len(batch), error=exc)
            logger.error("batch refused by the API, dropped %s", fields)
            return True
        self.delivered.delivered(result.stored, result.duplicates, len(result.rejected))
        for rejected in result.rejected:
            event = json.dumps(batch[rejected.index][1])[:200]
            logger.warning("event refused by the API %s", kv(reason=rejected.reason, event=event))
        logger.debug(
            "delivered %s",
            kv(path=path, items=len(batch), stored=result.stored, duplicates=result.duplicates),
        )
        return True

    def deliver_loop(self, stop: threading.Event) -> None:
        backoff = Backoff()
        while not stop.is_set():
            ok = self.deliver_once(self.measurements, MEASUREMENTS_PATH) and self.deliver_once(
                self.alerts, ALERTS_PATH
            )
            if ok:
                backoff.reset()
                stop.wait(IDLE_POLL_SECONDS)
            else:
                stop.wait(backoff.next_delay())

    def flush(self, timeout: float = SHUTDOWN_FLUSH_SECONDS) -> int:
        """On shutdown: try to deliver what is buffered; return how many events are left."""
        deadline = datetime.now(UTC).timestamp() + timeout
        for buffer, path in ((self.measurements, MEASUREMENTS_PATH), (self.alerts, ALERTS_PATH)):
            while len(buffer) and datetime.now(UTC).timestamp() < deadline:
                if not self.deliver_once(buffer, path, force=True):
                    break
        return len(self.measurements) + len(self.alerts)

    # Lifecycle -----------------------------------------------------------------------

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or shutdown_event()
        sender_stop = threading.Event()
        sender = threading.Thread(target=self.deliver_loop, args=(sender_stop,), daemon=True)
        sender.start()
        mqtt_helpers.start(self.client, self.mqtt_settings)
        try:
            while not stop.wait(STATUS_INTERVAL_SECONDS):
                if self.client.is_connected():
                    self.publish_status()
        finally:
            sender_stop.set()
            sender.join(timeout=SHUTDOWN_FLUSH_SECONDS)
            # Stop receiving first: with the persistent session, the broker keeps what
            # arrives from now on. Then hand what is buffered to the API.
            self.shutdown()
            left = self.flush()
            if left:
                logger.warning("events not delivered at shutdown %s", kv(count=left))
            self.delivery.close()

    def shutdown(self) -> None:
        if self.client.is_connected():
            try:
                self.publish_status(MonitorState.STOPPED, retain=False)
                cleared = self.client.publish(
                    self.status(MonitorState.STOPPED).topic, None, qos=STATUS_QOS, retain=True
                )
                cleared.wait_for_publish(2.0)
            except (RuntimeError, ValueError):
                logger.warning("status not cleared")
        mqtt_helpers.stop(self.client)


def main() -> int:
    try:
        configure_logging(log_level_from_env())
        mqtt_settings = MqttSettings.from_env()
        settings = IngestionSettings.from_env()
    except ConfigError as exc:
        logging.basicConfig()
        logger.error("invalid configuration: %s", exc)
        return 2
    logger.info(
        "ingestion starting %s",
        kv(client_id=CLIENT_ID, api=settings.api_url, batch=settings.batch_size,
           flush_s=settings.flush_interval, buffer=settings.buffer_limit,
           broker=f"{mqtt_settings.host}:{mqtt_settings.port}"),
    )  # fmt: skip
    Ingestion(settings, mqtt_settings, node=node_name()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
