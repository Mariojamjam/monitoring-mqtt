"""Paho MQTT client factory and connection helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from monitoring_mqtt.core.config import MqttSettings
from monitoring_mqtt.core.logging import kv

logger = logging.getLogger(__name__)

OnConnected = Callable[[mqtt.Client], None]


def create_client(
    client_id: str,
    settings: MqttSettings,
    on_connected: OnConnected | None = None,
) -> mqtt.Client:
    """Create an MQTT 3.1.1 client with automatic reconnection.

    Session type and reconnection backoff come from ``settings`` (experiment config).
    ``on_connected`` runs after every successful (re)connection. With clean sessions the
    broker forgets subscriptions on disconnect, so subscribers must subscribe there; with
    persistent sessions re-subscribing is harmless.
    """
    client = mqtt.Client(
        CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv311,  # protocol version is part of the v1 contract, not a parameter
        clean_session=settings.clean_session,
    )
    client.reconnect_delay_set(settings.reconnect_min_delay, settings.reconnect_max_delay)

    def handle_connect(client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            logger.warning("mqtt connect failed %s", kv(client_id=client_id, reason=reason_code))
            return
        logger.info("mqtt connected %s", kv(client_id=client_id))
        if on_connected is not None:
            on_connected(client)

    def handle_disconnect(client, userdata, flags, reason_code, properties) -> None:
        log = logger.warning if reason_code.is_failure else logger.info
        log("mqtt disconnected %s", kv(client_id=client_id, reason=reason_code))

    client.on_connect = handle_connect
    client.on_disconnect = handle_disconnect
    return client


def start(client: mqtt.Client, settings: MqttSettings) -> None:
    """Connect asynchronously and start the network thread.

    The network thread keeps retrying until the broker is reachable, so services
    tolerate a broker that starts later or restarts.
    """
    client.connect_async(settings.host, settings.port, keepalive=settings.keepalive)
    client.loop_start()


def stop(client: mqtt.Client) -> None:
    """Disconnect cleanly and stop the network thread."""
    client.disconnect()
    client.loop_stop()
