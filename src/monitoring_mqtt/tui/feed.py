"""Observer MQTT client: subscribes to everything the TUI shows and queues raw messages.

The observer is an ordinary subscriber with its own client id (``tui-<id>``) and a clean
session. It never publishes. Paho callbacks only enqueue; the UI thread drains the queue
and updates the model, so the model needs no locking.
"""

from __future__ import annotations

import queue
import time
import uuid
from dataclasses import replace

import paho.mqtt.client as mqtt

from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.config import MqttSettings
from monitoring_mqtt.core.registry import CLAIM_QOS, WORKER_CLAIMS_FILTER
from monitoring_mqtt.core.simulation import SIM_EVENTS_FILTER, SIM_QOS
from monitoring_mqtt.core.status import MONITOR_STATUS_FILTER, STATUS_QOS
from monitoring_mqtt.core.topics import ALL_TELEMETRY_FILTER
from monitoring_mqtt.tui.state import Connection, FeedItem, Message

BROKER_STATS_FILTER = "$SYS/broker/#"
MAX_DRAIN = 50_000


def subscriptions(telemetry_qos: int) -> list[tuple[str, int]]:
    return [
        (ALL_TELEMETRY_FILTER, telemetry_qos),
        (WORKER_CLAIMS_FILTER, CLAIM_QOS),
        (MONITOR_STATUS_FILTER, STATUS_QOS),
        (SIM_EVENTS_FILTER, SIM_QOS),
        (BROKER_STATS_FILTER, 0),
    ]


class Feed:
    def __init__(self, settings: MqttSettings, client_id: str | None = None) -> None:
        self.settings = replace(settings, clean_session=True)
        self.client_id = client_id or f"tui-{uuid.uuid4().hex[:8]}"
        self.address = f"{settings.host}:{settings.port}"
        self.queue: queue.SimpleQueue[FeedItem] = queue.SimpleQueue()
        self.client = mqtt_helpers.create_client(
            self.client_id, self.settings, on_connected=self._on_connected
        )
        self.client.on_message = self._on_message
        log_disconnect = self.client.on_disconnect

        def on_disconnect(client, userdata, flags, reason_code, properties) -> None:
            log_disconnect(client, userdata, flags, reason_code, properties)
            self.queue.put(Connection(False, time.time(), str(reason_code)))

        self.client.on_disconnect = on_disconnect

    def _on_connected(self, client: mqtt.Client) -> None:
        client.subscribe(subscriptions(self.settings.qos))
        self.queue.put(Connection(True, time.time(), self.address))

    def _on_message(self, client, userdata, message: mqtt.MQTTMessage) -> None:
        self.queue.put(Message(message.topic, message.payload, time.time()))

    def start(self) -> None:
        mqtt_helpers.start(self.client, self.settings)

    def stop(self) -> None:
        mqtt_helpers.stop(self.client)

    def drain(self, limit: int = MAX_DRAIN) -> list[FeedItem]:
        items: list[FeedItem] = []
        try:
            while len(items) < limit:
                items.append(self.queue.get_nowait())
        except queue.Empty:
            pass
        return items
