"""Virtual device: one identity, one MQTT connection, one generator."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.config import MqttSettings
from monitoring_mqtt.core.logging import kv
from monitoring_mqtt.core.models import Measurement
from monitoring_mqtt.simulator.generators.base import MeasurementGenerator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceProfile:
    device_type: str
    domain: str
    metric: str
    unit: str | None


class VirtualDevice:
    """Publishes measurements over its own MQTT client.

    The sequence counter increases for every generated measurement, including those
    that could not be published while disconnected, so gaps reveal offline periods.
    """

    def __init__(
        self,
        device_id: str,
        profile: DeviceProfile,
        site_id: str,
        run_id: str,
        generator: MeasurementGenerator,
        settings: MqttSettings,
        client: mqtt.Client | None = None,
    ) -> None:
        self.device_id = device_id
        self.profile = profile
        self.site_id = site_id
        self.run_id = run_id
        self.generator = generator
        self.settings = settings
        self.client = client or mqtt_helpers.create_client(device_id, settings)
        self.sequence = 0

    def start(self) -> None:
        mqtt_helpers.start(self.client, self.settings)

    def stop(self) -> None:
        mqtt_helpers.stop(self.client)

    def go_offline(self) -> None:
        """Disconnect (a simulated stop or disconnection); the worker keeps running."""
        mqtt_helpers.stop(self.client)

    def go_online(self) -> None:
        mqtt_helpers.start(self.client, self.settings)

    @property
    def connected(self) -> bool:
        return self.client.is_connected()

    def next_measurement(self) -> Measurement:
        self.sequence += 1
        return Measurement.create(
            run_id=self.run_id,
            device_id=self.device_id,
            device_type=self.profile.device_type,
            domain=self.profile.domain,
            site_id=self.site_id,
            metric=self.profile.metric,
            value=self.generator.next_value(),
            unit=self.profile.unit,
            sequence=self.sequence,
        )

    def publish_next(self) -> Measurement | None:
        """Generate and publish one measurement; return it if handed to the client."""
        return self.publish(self.next_measurement())

    def publish(self, measurement: Measurement) -> Measurement | None:
        """Publish an already generated measurement; return it if handed to the client."""
        fields = kv(device_id=self.device_id, seq=measurement.sequence)
        if not self.client.is_connected():
            logger.warning("measurement dropped, broker unavailable %s", fields)
            return None

        info = self.client.publish(
            measurement.topic, measurement.to_payload(), qos=self.settings.qos, retain=False
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.warning("publish failed %s rc=%s", fields, mqtt.error_string(info.rc))
            return None

        logger.info(
            "published %s",
            kv(
                topic=measurement.topic,
                seq=measurement.sequence,
                value=measurement.value,
                unit=measurement.unit,
                event_id=measurement.event_id,
            ),
        )
        return measurement
