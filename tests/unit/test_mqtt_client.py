from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.config import MqttSettings


class FakeClient:
    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs
        self.reconnect_delay = None

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        self.reconnect_delay = (min_delay, max_delay)


def test_client_uses_session_and_backoff_from_settings(monkeypatch) -> None:
    monkeypatch.setattr(mqtt_helpers.mqtt, "Client", FakeClient)
    settings = MqttSettings(clean_session=False, reconnect_min_delay=2, reconnect_max_delay=8)
    client = mqtt_helpers.create_client("poultry-abc123-001", settings)
    assert client.kwargs["client_id"] == "poultry-abc123-001"
    assert client.kwargs["clean_session"] is False
    assert client.kwargs["protocol"] == mqtt_helpers.mqtt.MQTTv311
    assert client.reconnect_delay == (2, 8)
