import json
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt

from monitoring_mqtt.core.config import MqttSettings
from monitoring_mqtt.simulator.device import DeviceProfile, VirtualDevice

PROFILE = DeviceProfile("dissolved_oxygen_probe", "aquaculture", "dissolved_oxygen", "mg/L")


@dataclass
class FakeInfo:
    rc: int = mqtt.MQTT_ERR_SUCCESS


@dataclass
class FakeClient:
    connected: bool = True
    published: list[tuple] = field(default_factory=list)

    def is_connected(self) -> bool:
        return self.connected

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> FakeInfo:
        self.published.append((topic, payload, qos, retain))
        return FakeInfo()


class ConstantGenerator:
    def next_value(self) -> float:
        return 6.5


def make_device(client: FakeClient) -> VirtualDevice:
    return VirtualDevice(
        device_id="aquaculture-abc123-001",
        profile=PROFILE,
        site_id="pond-01",
        run_id="run-1",
        generator=ConstantGenerator(),
        settings=MqttSettings(qos=1),
        client=client,  # type: ignore[arg-type]
    )


def test_publishes_contract_payload_on_device_topic() -> None:
    client = FakeClient()
    measurement = make_device(client).publish_next()

    assert measurement is not None
    topic, payload, qos, retain = client.published[0]
    assert topic == "monitoring/v1/aquaculture/dissolved_oxygen/aquaculture-abc123-001"
    assert (qos, retain) == (1, False)
    body = json.loads(payload)
    assert body["device_id"] == "aquaculture-abc123-001"
    assert body["value"] == 6.5
    assert body["sequence"] == 1


def test_sequence_increases_and_gaps_when_disconnected() -> None:
    client = FakeClient()
    device = make_device(client)
    device.publish_next()
    client.connected = False
    assert device.publish_next() is None
    client.connected = True
    device.publish_next()

    sequences = [json.loads(p[1])["sequence"] for p in client.published]
    assert sequences == [1, 3]
