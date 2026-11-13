"""Alert engine: ``python -m monitoring_mqtt.consumers.alerts``.

An independent subscriber with business logic: it applies the rules (``rules.py``) to every
measurement and publishes **derived events** on the broker (contract in
``core/alerts.py``). It knows nothing about the database or the API; the ingestion consumer
stores its alerts like any other event.

Restarts keep the alert state:

1. before connecting, a short-lived reader collects the retained *active* alerts and the
   engine restores them, so an open alert is neither raised again nor forgotten;
2. then the engine connects with a fixed client id (``alerts``) and a persistent session:
   measurements published while it was down were queued by the broker and are evaluated
   now. Only one instance may run with this id.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from monitoring_mqtt.consumers.monitor import InvalidMessageError, MonitorStats, decode_measurement
from monitoring_mqtt.consumers.rules import (
    DEFAULT_RULES_PATH,
    AlertEvaluator,
    RulesError,
    RulesFile,
    load_rules,
)
from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.alerts import ACTIVE_ALERTS_FILTER, ALERT_QOS, AlertEvent, AlertState
from monitoring_mqtt.core.config import ConfigError, MqttSettings, log_level_from_env
from monitoring_mqtt.core.lifecycle import shutdown_event
from monitoring_mqtt.core.logging import configure_logging, kv
from monitoring_mqtt.core.status import (
    STATUS_INTERVAL_SECONDS,
    STATUS_QOS,
    AlertCounters,
    MonitorState,
    MonitorStatus,
    node_name,
)
from monitoring_mqtt.core.topics import ALL_TELEMETRY_FILTER, SMOKE_DOMAIN

logger = logging.getLogger("monitoring_mqtt.alerts")

CLIENT_ID = "alerts"  # fixed: the broker keeps this session while the engine is down
RESTORE_TIMEOUT_SECONDS = 10.0
RESTORE_COLLECT_SECONDS = 1.0  # retained messages arrive right after the subscription


def read_active_alerts(settings: MqttSettings, timeout: float = RESTORE_TIMEOUT_SECONDS):
    """Open alerts from the retained active topics, or ``None`` if the broker is unreachable."""
    found: dict[str, AlertEvent] = {}
    lock = threading.Lock()
    subscribed = threading.Event()

    def on_connected(client: mqtt.Client) -> None:
        client.subscribe(ACTIVE_ALERTS_FILTER, qos=ALERT_QOS)

    def on_subscribe(client, userdata, mid, reason_codes, properties) -> None:
        subscribed.set()

    def on_message(client, userdata, message: mqtt.MQTTMessage) -> None:
        if not message.payload:
            return
        try:
            event = AlertEvent.from_payload(message.payload)
        except ValidationError:
            logger.warning("invalid active alert ignored %s", kv(topic=message.topic))
            return
        with lock:
            found[message.topic] = event

    reader_settings = replace(settings, clean_session=True)
    client = mqtt_helpers.create_client(
        f"alerts-restore-{uuid.uuid4().hex[:8]}", reader_settings, on_connected=on_connected
    )
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    mqtt_helpers.start(client, reader_settings)
    try:
        if not subscribed.wait(timeout):
            return None
        time.sleep(RESTORE_COLLECT_SECONDS)
        with lock:
            return list(found.values())
    finally:
        mqtt_helpers.stop(client)


class AlertEngine:
    def __init__(
        self,
        rules: RulesFile,
        settings: MqttSettings,
        node: str | None = None,
        hostname: str | None = None,
        client: mqtt.Client | None = None,
    ) -> None:
        self.rules = rules
        self.settings = replace(settings, clean_session=False)
        self.evaluator = AlertEvaluator(rules.rules)
        self.node = node
        self.hostname = hostname or socket.gethostname()
        self.started_at = datetime.now(UTC)
        self.stats = MonitorStats()
        self._lock = threading.Lock()
        self.raised = 0
        self.cleared = 0
        self.client = client or mqtt_helpers.create_client(
            CLIENT_ID, self.settings, on_connected=self._on_connected
        )
        self.client.on_message = self._on_message
        offline = self.status(MonitorState.OFFLINE, counters=False)
        self.client.will_set(offline.topic, offline.to_payload(), qos=STATUS_QOS, retain=True)

    def restore(self, events: list[AlertEvent]) -> None:
        for event in events:
            self.evaluator.restore(event)
        if events:
            logger.info("open alerts restored %s", kv(count=len(events)))

    # Status --------------------------------------------------------------------------

    def status(self, state: MonitorState, counters: bool = True) -> MonitorStatus:
        alerts = None
        if counters:
            with self._lock:
                open_alerts = self.evaluator.open_alerts()
                alerts = AlertCounters(
                    open=len(open_alerts),
                    raised=self.raised,
                    cleared=self.cleared,
                    open_by_severity=dict(Counter(a.severity.value for a in open_alerts)),
                )
        return MonitorStatus(
            client_id=CLIENT_ID,
            monitor="alerts",
            topic_filter=ALL_TELEMETRY_FILTER,
            node=self.node,
            hostname=self.hostname,
            state=state,
            started_at=self.started_at,
            updated_at=datetime.now(UTC),
            counters=self.stats.snapshot() if counters else None,
            alerts=alerts,
        )

    def publish_status(self, state: MonitorState = MonitorState.ONLINE, retain: bool = True):
        status = self.status(state)
        return self.client.publish(status.topic, status.to_payload(), qos=STATUS_QOS, retain=retain)

    # Network thread ------------------------------------------------------------------

    def _on_connected(self, client: mqtt.Client) -> None:
        client.subscribe(ALL_TELEMETRY_FILTER, qos=1)
        logger.info("subscribed %s", kv(filter=ALL_TELEMETRY_FILTER, rules=len(self.rules.rules)))
        self.publish_status()

    def _on_message(self, client: mqtt.Client, userdata: object, message: mqtt.MQTTMessage) -> None:
        try:
            self.handle(message.topic, message.payload)
        except Exception:
            logger.exception("unexpected error handling message %s", kv(topic=message.topic))

    def handle(self, topic: str, payload: bytes) -> list[AlertEvent]:
        try:
            measurement = decode_measurement(topic, payload)
        except InvalidMessageError as exc:
            self.stats.record_invalid()
            logger.debug("invalid message ignored %s error=%s", kv(topic=topic), exc)
            return []
        if measurement.domain == SMOKE_DOMAIN:
            return []
        now = datetime.now(UTC)
        self.stats.record(topic, measurement, (now - measurement.timestamp).total_seconds() * 1000,
                          now)  # fmt: skip
        with self._lock:
            events = self.evaluator.evaluate(measurement)
            for event in events:
                if event.state is AlertState.RAISED:
                    self.raised += 1
                else:
                    self.cleared += 1
        for event in events:
            self.publish(event)
        return events

    def publish(self, event: AlertEvent) -> None:
        self.client.publish(event.topic, event.to_payload(), qos=ALERT_QOS, retain=False)
        active = event.to_payload() if event.state is AlertState.RAISED else None
        self.client.publish(event.active_topic, active, qos=ALERT_QOS, retain=True)
        logger.warning(
            "alert %s %s",
            event.state.value,
            kv(
                rule=event.rule_id, severity=event.severity.value, device_id=event.device_id,
                value=event.value, threshold=event.threshold, alert_id=event.alert_id,
            ),
        )  # fmt: skip

    # Lifecycle -----------------------------------------------------------------------

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or shutdown_event()
        mqtt_helpers.start(self.client, self.settings)
        try:
            while not stop.wait(STATUS_INTERVAL_SECONDS):
                if self.client.is_connected():
                    self.publish_status()
        finally:
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


def load_configured_rules() -> RulesFile:
    """``ALERT_RULES`` (JSON, set by the CLI from the experiment) or the rules file."""
    raw = os.environ.get("ALERT_RULES", "").strip()
    if raw:
        try:
            return RulesFile.from_env(raw)
        except ValidationError as exc:
            raise RulesError(f"invalid ALERT_RULES: {exc}") from exc
    return load_rules(Path(os.environ.get("ALERT_RULES_FILE", str(DEFAULT_RULES_PATH))))


def main() -> int:
    try:
        configure_logging(log_level_from_env())
        settings = MqttSettings.from_env()
        rules = load_configured_rules()
    except (ConfigError, RulesError) as exc:
        logging.basicConfig()
        logger.error("invalid configuration: %s", exc)
        return 2
    engine = AlertEngine(rules, settings, node=node_name())
    active = read_active_alerts(settings)
    if active is None:
        logger.warning("broker unreachable while restoring alerts; starting without them")
    else:
        engine.restore(active)
    logger.info(
        "alert engine starting %s",
        kv(rules=len(rules.rules), broker=f"{settings.host}:{settings.port}"),
    )
    engine.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
