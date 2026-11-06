import pytest

from monitoring_mqtt.consumers.monitor import InvalidMessageError, Monitor, decode_measurement
from monitoring_mqtt.core.config import MqttSettings


def test_decode_valid_message(measurement) -> None:
    m = measurement()
    assert decode_measurement(m.topic, m.to_payload()) == m


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        (None, b"not json"),
        (None, b"{}"),
        ("monitoring/v1/poultry/dissolved_oxygen/aquaculture-abc123-001", None),  # mismatch
        ("monitoring/v1/aquaculture/short", None),  # malformed topic
    ],
)
def test_decode_invalid_messages(measurement, topic: str | None, payload: bytes | None) -> None:
    m = measurement()
    with pytest.raises(InvalidMessageError):
        decode_measurement(topic or m.topic, payload or m.to_payload())


def test_monitor_counts_valid_and_invalid_messages(measurement) -> None:
    m = measurement()
    monitor = Monitor("aquaculture", "monitoring/v1/aquaculture/#", MqttSettings())
    assert monitor.handle(m.topic, b"garbage") is None
    assert monitor.handle(m.topic, m.to_payload()) == m
    assert (monitor.stats.received, monitor.stats.invalid) == (1, 1)
