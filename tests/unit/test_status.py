from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from monitoring_mqtt.consumers import monitor as monitor_module
from monitoring_mqtt.consumers.monitor import Monitor
from monitoring_mqtt.core.config import MqttSettings
from monitoring_mqtt.core.status import (
    MONITOR_STATUS_FILTER,
    MonitorState,
    MonitorStatus,
    monitor_status_topic,
    node_name,
)
from monitoring_mqtt.core.topics import ALL_TELEMETRY_FILTER, matches


def test_status_topic_is_outside_telemetry() -> None:
    topic = monitor_status_topic("monitor-poultry-0279a8")
    assert topic == "monitoring/status/v1/monitors/monitor-poultry-0279a8"
    assert matches(MONITOR_STATUS_FILTER, topic)
    assert not matches(ALL_TELEMETRY_FILTER, topic)  # monitors never receive status


def test_node_name_from_environment() -> None:
    assert node_name({"NODE_NAME": " pi "}) == "pi"
    assert node_name({}) is None


class FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, int, bool]] = []
        self.will = None
        self.connected = True

    def will_set(self, topic, payload, qos, retain) -> None:
        self.will = (topic, payload, qos, retain)

    def subscribe(self, topic, qos) -> None:
        pass

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(wait_for_publish=lambda timeout: None)

    def is_connected(self) -> bool:
        return self.connected


@pytest.fixture
def monitor(monkeypatch: pytest.MonkeyPatch) -> Monitor:
    fake = FakeClient()
    monkeypatch.setattr(monitor_module.mqtt_helpers, "create_client", lambda *a, **k: fake)
    monkeypatch.setattr(monitor_module.mqtt_helpers, "stop", lambda client: None)
    return Monitor(
        "poultry", "monitoring/v1/poultry/#", MqttSettings(), node="pc-lab", hostname="0279a8ffff00"
    )


def statuses(monitor: Monitor) -> list[tuple[MonitorStatus | None, bool]]:
    return [
        (MonitorStatus.from_payload(p) if p else None, retain)
        for _, p, _, retain in monitor.client.published
    ]


def test_last_will_announces_offline(monitor: Monitor) -> None:
    topic, payload, qos, retain = monitor.client.will
    status = MonitorStatus.from_payload(payload)
    assert topic == monitor_status_topic("monitor-poultry-0279a8") and qos == 1 and retain
    assert status.state is MonitorState.OFFLINE
    assert status.counters is None
    assert (status.node, status.topic_filter) == ("pc-lab", "monitoring/v1/poultry/#")


def test_online_status_on_connect_carries_counters(monitor: Monitor, measurement) -> None:
    m = measurement(device_id="poultry-abc123-001", domain="poultry", metric="ammonia")
    monitor.handle(m.topic, m.to_payload())
    monitor.handle(m.topic, b"garbage")
    monitor._on_connected(monitor.client)
    [(status, retain)] = statuses(monitor)
    assert retain and status.state is MonitorState.ONLINE
    counters = status.counters
    assert (counters.received, counters.invalid, counters.devices_seen) == (1, 1, 1)
    assert counters.by_domain == {"poultry": 1}
    assert counters.latency_ms is not None and counters.last_received_at is not None
    [message] = counters.recent  # what this monitor actually received
    assert (message.topic, message.device_id, message.sequence) == (m.topic, m.device_id, 1)
    assert message.event_id == m.event_id


def test_clean_shutdown_announces_stop_and_removes_status(monitor: Monitor) -> None:
    monitor.shutdown()
    (stopped, stopped_retained), (cleared, cleared_retained) = statuses(monitor)
    assert stopped.state is MonitorState.STOPPED and not stopped_retained
    assert cleared is None and cleared_retained  # empty retained payload removes it


def test_status_round_trip(monitor: Monitor) -> None:
    status = monitor.status(MonitorState.ONLINE)
    assert MonitorStatus.from_payload(status.to_payload()) == status
    assert status.started_at.tzinfo == UTC and status.updated_at >= status.started_at
    assert isinstance(status.started_at, datetime)


def test_recent_messages_are_bounded(monitor: Monitor, measurement) -> None:
    for seq in range(1, 16):
        m = measurement(sequence=seq)
        monitor.handle(m.topic, m.to_payload())
    recent = monitor.status(MonitorState.ONLINE).counters.recent
    assert [r.sequence for r in recent] == list(range(6, 16))  # latest 10, oldest first
