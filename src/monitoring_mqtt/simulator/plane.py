"""The worker's connection to the simulation plane (contract in ``core/simulation.py``).

One extra MQTT connection per worker, separate from its devices: it publishes ground truth
events and receives live commands for the worker's devices. Devices stay pure publishers;
a device that is simulating a disconnection cannot carry its own events anyway.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.config import MqttSettings
from monitoring_mqtt.core.logging import kv
from monitoring_mqtt.core.simulation import (
    SIM_QOS,
    ControlAck,
    ControlCommand,
    SimEvent,
    ack_topic,
    worker_control_filter,
)

logger = logging.getLogger(__name__)

# Receives a command for a device; returns (accepted, detail).
CommandHandler = Callable[[str, ControlCommand], tuple[bool, str]]


class SimulationPlane:
    def __init__(
        self,
        worker_id: str,
        settings: MqttSettings,
        on_command: CommandHandler,
        client: mqtt.Client | None = None,
    ) -> None:
        self.worker_id = worker_id
        self._settings = replace(settings, clean_session=True)  # never queue old commands
        self._on_command = on_command
        self.client = client or mqtt_helpers.create_client(
            f"sim-{worker_id}", self._settings, self._on_connected
        )
        self.client.on_message = self._on_message

    def start(self) -> None:
        mqtt_helpers.start(self.client, self._settings)

    def stop(self) -> None:
        mqtt_helpers.stop(self.client)

    def publish_event(self, event: SimEvent) -> None:
        self.client.publish(event.topic, event.to_payload(), qos=SIM_QOS, retain=False)

    def _on_connected(self, client: mqtt.Client) -> None:
        client.subscribe(worker_control_filter(self.worker_id), qos=SIM_QOS)

    def _on_message(self, client, userdata, message: mqtt.MQTTMessage) -> None:
        try:
            self.handle(message.topic, message.payload)
        except Exception:
            logger.exception("control command handling failed %s", kv(topic=message.topic))

    def handle(self, topic: str, payload: bytes) -> ControlAck | None:
        device_id = topic.rsplit("/", 1)[-1]
        try:
            command = ControlCommand.from_payload(payload)
        except ValidationError as exc:
            logger.warning(
                "invalid control command ignored %s",
                kv(topic=topic, error=exc.errors()[0]["msg"]),
            )
            return None
        if command.device_id != device_id:
            accepted, detail = False, "device id in the payload does not match the topic"
        else:
            accepted, detail = self._on_command(device_id, command)
        logger.info(
            "control command %s",
            kv(
                device_id=device_id,
                action=command.action,
                lasts_s=command.lasts_seconds,
                accepted=accepted,
                request_id=command.request_id,
            ),
        )
        ack = ControlAck(
            request_id=command.request_id,
            device_id=device_id,
            worker_id=self.worker_id,
            accepted=accepted,
            detail=detail,
        )
        self.client.publish(ack_topic(command.request_id), ack.to_payload(), qos=SIM_QOS)
        return ack
