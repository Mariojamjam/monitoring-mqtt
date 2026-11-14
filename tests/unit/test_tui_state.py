import uuid
from datetime import timedelta

import pytest
from tui_messages import E0, T0, claim, status, telemetry

from monitoring_mqtt.core.models import Measurement
from monitoring_mqtt.core.simulation import EpisodeKind, EpisodePhase, EpisodeSource, SimEvent
from monitoring_mqtt.core.status import (
    MonitorState,
    MonitorStatus,
    ReceivedMessage,
    monitor_status_topic,
)
from monitoring_mqtt.tui.feed import subscriptions
from monitoring_mqtt.tui.state import (
    Band,
    Connection,
    EventKind,
    Health,
    Message,
    SystemState,
    worker_of,
)


@pytest.fixture
def state() -> SystemState:
    return SystemState(publish_interval=5.0)


def test_worker_id_from_device_id() -> None:
    assert worker_of("poultry-fc5336-001", "poultry") == "fc5336"
    assert worker_of("aquaculture-a-b-c-012", "aquaculture") == "a-b-c"


def test_observer_subscribes_to_every_plane() -> None:
    topics = [t for t, _ in subscriptions(1)]
    assert topics == [
        "monitoring/v1/#",
        "monitoring/registry/v1/workers/+",
        "monitoring/status/v1/monitors/+",
        "monitoring/sim/v1/devices/+",
        "monitoring/alerts/v1/#",
        "$SYS/broker/#",
    ]


def test_devices_values_rates_and_sequence_anomalies(state: SystemState) -> None:
    state.apply_all(
        [
            claim("fc5336"),
            telemetry("poultry-fc5336-001", 1, E0, value=10),
            telemetry("poultry-fc5336-001", 2, E0 + 5, value=11),
            telemetry("poultry-fc5336-001", 5, E0 + 10, value=12),  # 3 and 4 lost
            telemetry("poultry-fc5336-001", 5, E0 + 10.1),  # QoS 1 duplicate
            telemetry("poultry-fc5336-001", 1, E0 + 15),  # restarted
        ]
    )
    device = state.devices["poultry-fc5336-001"]
    assert (device.received, device.missing, device.duplicates, device.restarts) == (5, 2, 1, 1)
    assert list(device.values)[:3] == [10, 11, 12]
    assert device.worker_id == "fc5336" and state.node_of(device) == "pc-1"
    assert device.rate(E0 + 15) == pytest.approx(0.4)  # 4 arrivals in the last 10 s
    assert 1 < device.latency_ms < 3
    assert state.run_id == "lab-01"
    kinds = [e.kind for e in state.notable]
    assert EventKind.TELEMETRY not in kinds  # system changes only
    assert EventKind.GAP in kinds and EventKind.RESTART in kinds and EventKind.DUPLICATE in kinds
    assert state.missing_total() == 2


def test_device_health(state: SystemState) -> None:
    state.apply_all([claim("fc5336"), telemetry("poultry-fc5336-001", 1, E0)])
    device = state.devices["poultry-fc5336-001"]
    assert state.device_health(device, E0 + 10) is Health.ONLINE  # within 2.5 intervals
    assert state.device_health(device, E0 + 20) is Health.STALE
    state.apply(Message("monitoring/registry/v1/workers/fc5336", b"", E0 + 21))
    assert state.device_health(device, E0 + 30) is Health.GONE  # its worker left
    assert state.events[-1].kind is EventKind.WORKER_LEFT


def test_invalid_messages_and_smoke_probes_are_not_devices(state: SystemState) -> None:
    probe = Measurement.create(
        run_id="smoke-1", device_id="smoke-1", device_type="smoke_probe", domain="smoke",
        site_id="smoke", metric="probe", value=1, unit=None, sequence=1,
    )  # fmt: skip
    state.apply_all(
        [
            Message("monitoring/v1/poultry/ammonia/x-1", b"not json", E0),
            Message(probe.topic, probe.to_payload(), E0),
        ]
    )
    assert state.devices == {}
    assert (state.invalid, state.probes) == (1, 1)
    assert state.events[0].kind is EventKind.INVALID and state.events[0].payload == "not json"


def test_routing_follows_online_filters(state: SystemState) -> None:
    poultry = "monitoring/v1/poultry/#"
    state.apply_all(
        [
            telemetry("poultry-aaaaaa-001", 1, E0),
            telemetry("cattle-bbbbbb-001", 1, E0, domain="cattle"),
            status("monitor-poultry-000001", poultry, E0, received=10, poultry=10),
            status("monitor-poultry-000002", poultry, E0, received=10, poultry=10),
            status("monitor-all-000003", "monitoring/v1/#", E0, received=20, poultry=10, cattle=10),
        ]
    )
    poultry_device = state.devices["poultry-aaaaaa-001"]
    cattle_device = state.devices["cattle-bbbbbb-001"]
    assert len(state.consumers_of(poultry_device)) == 3
    assert [m.client_id for m in state.consumers_of(cattle_device)] == ["monitor-all-000003"]

    # monitor-all goes offline: cattle keeps publishing, nobody receives it.
    state.apply(status("monitor-all-000003", "monitoring/v1/#", E0 + 1, MonitorState.OFFLINE))
    assert state.consumers_of(cattle_device) == []


def test_what_each_monitor_received(state: SystemState) -> None:
    message = status("monitor-poultry-000001", "monitoring/v1/poultry/#", E0, received=2)
    payload = MonitorStatus.from_payload(message.payload)
    received = [
        ReceivedMessage(
            topic=f"monitoring/v1/poultry/ammonia/poultry-aaaaaa-00{i}",
            device_id=f"poultry-aaaaaa-00{i}",
            value=10 + i, unit="ppm", sequence=i, event_id=uuid.uuid4(),
            received_at=T0, latency_ms=1.5,
        )
        for i in (1, 2)
    ]  # fmt: skip
    counters = payload.counters.model_copy(update={"recent": received})
    status_with_recent = payload.model_copy(update={"counters": counters})
    state.apply(Message(message.topic, status_with_recent.to_payload(), E0))
    monitor = state.monitors["monitor-poultry-000001"]
    assert [m.sequence for m in monitor.recent] == [2, 1]  # newest first
    assert monitor.status.topic_filter == "monitoring/v1/poultry/#"


def test_monitor_lifecycle_and_rate(state: SystemState) -> None:
    topic_filter = "monitoring/v1/poultry/#"
    state.apply(status("monitor-poultry-000001", topic_filter, E0, received=10, poultry=10))
    state.apply(status("monitor-poultry-000001", topic_filter, E0 + 5, received=30, poultry=30))
    monitor = state.monitors["monitor-poultry-000001"]
    assert monitor.rate == pytest.approx(4.0)
    assert monitor.health(E0 + 6) is MonitorState.ONLINE
    assert monitor.health(E0 + 30) is Health.STALE  # no status for 3 intervals
    state.apply(Message(monitor_status_topic("monitor-poultry-000001"), b"", E0 + 31))
    assert state.monitors["monitor-poultry-000001"].state is MonitorState.STOPPED
    assert [e.kind for e in state.events] == [EventKind.MONITOR_ONLINE]


def test_broker_statistics_and_connection(state: SystemState) -> None:
    state.apply_all(
        [
            Connection(True, E0, "broker:1883"),
            Message("$SYS/broker/version", b"mosquitto version 2.1.2", E0),
            Message("$SYS/broker/uptime", b"3600 seconds", E0),
            Message("$SYS/broker/clients/connected", b"10", E0),
            Message("$SYS/broker/load/publish/received/1min", b"36.0", E0),
            Connection(False, E0 + 1, "connection lost"),
        ]
    )
    broker = state.broker
    assert broker.version == "mosquitto version 2.1.2"
    assert (broker.uptime_seconds, broker.clients) == (3600, 10)
    assert broker.per_second("received") == pytest.approx(0.6)
    assert broker.per_second("sent") is None
    assert not broker.connected and broker.disconnects == 1


def test_flow_buckets(state: SystemState) -> None:
    state.apply_all([telemetry("poultry-aaaaaa-001", i, E0 + i) for i in range(1, 11)])
    flow = state.flow("poultry", E0 + 10, buckets=6)  # 10 s per bucket
    assert sum(flow) == 10 and flow[-1] == 10
    assert state.domain_rate("poultry", E0 + 10) == pytest.approx(1.0)


def test_own_worker_id_is_flagged(state: SystemState) -> None:
    state.apply_all([claim("fc5336"), claim("3cb001", hostname="4913f2f3e94f")])
    assert not state.workers["fc5336"].own_id
    assert state.workers["3cb001"].own_id


def test_version_changes_on_every_item(state: SystemState) -> None:
    before = state.version
    state.apply(Connection(True, E0))
    assert state.version == before + 1


def test_status_updated_at_is_used_for_rates(state: SystemState) -> None:
    topic_filter = "monitoring/v1/#"
    state.apply(status("monitor-all-000001", topic_filter, E0, received=0))
    later = E0 + timedelta(seconds=10).total_seconds()
    state.apply(status("monitor-all-000001", topic_filter, later, received=50))
    assert state.monitors["monitor-all-000001"].rate == pytest.approx(5.0)


def test_monitor_restart_resets_rate_baseline(state: SystemState) -> None:
    topic_filter = "monitoring/v1/poultry/#"
    state.apply(status("monitor-poultry-000001", topic_filter, E0, received=100))
    restarted = status("monitor-poultry-000001", topic_filter, E0 + 5, received=40)
    # Same client id, later process start: counters restarted from zero.
    restarted = Message(
        restarted.topic,
        restarted.payload.replace(b'"started_at":"2026-11-06T12:00:00Z"',
                                  b'"started_at":"2026-11-06T12:00:04Z"'),
        restarted.received_at,
    )  # fmt: skip
    state.apply(restarted)
    assert state.monitors["monitor-poultry-000001"].rate == 0.0  # not a negative delta
    assert state.notable[-1].text.endswith("(restarted)")


def sim(device_id: str, kind: str, phase: str, at: float, **extra) -> Message:
    event = SimEvent(
        run_id="lab-01", worker_id="fc5336", device_id=device_id, kind=EpisodeKind(kind),
        phase=EpisodePhase(phase), source=EpisodeSource.FAULT, **extra,
    )  # fmt: skip
    return Message(event.topic, event.to_payload(), at)


def test_simulated_episodes_and_normal_band() -> None:
    state = SystemState(5.0, bands={"poultry_type": Band(0.0, 25.0)})
    device_id = "poultry-fc5336-001"
    state.apply_all(
        [
            claim("fc5336"),
            telemetry(device_id, 1, E0, value=40),
            sim(device_id, "anomaly", "start", E0 + 1, direction="high"),
            sim(device_id, "stop", "start", E0 + 2),
        ]
    )
    device = state.devices[device_id]
    assert state.out_of_range(device) is True
    assert [e.kind for e in state.mode_of(device_id).active] == [
        EpisodeKind.STOP,
        EpisodeKind.ANOMALY,
    ]
    assert state.notable[-1].kind is EventKind.SIMULATED
    assert state.notable[-1].text == "stop started (fault)"

    state.apply(sim(device_id, "stop", "end", E0 + 3))
    assert [e.kind for e in state.mode_of(device_id).active] == [EpisodeKind.ANOMALY]
    # The worker leaves: its devices' episodes end with it.
    state.apply(Message("monitoring/registry/v1/workers/fc5336", b"", E0 + 4))
    assert state.mode_of(device_id).active == []


def test_no_band_means_unknown_range(state: SystemState) -> None:
    state.apply(telemetry("poultry-fc5336-001", 1, E0, value=99))
    assert state.out_of_range(state.devices["poultry-fc5336-001"]) is None


def alert_message(state: str, at: float, active: bool = False) -> Message:
    from monitoring_mqtt.consumers.rules import AlertEvaluator
    from monitoring_mqtt.core.alerts import AlertState
    from monitoring_mqtt.core.rules import Rule

    evaluator = AlertEvaluator([Rule(id="ammonia-high", device_type="poultry_type", above=25)])
    m = Measurement.from_payload(telemetry("poultry-fc5336-001", 1, E0, value=30).payload)
    event = evaluator.evaluate(m)[0]
    if state == "cleared":
        event = event.model_copy(update={"state": AlertState.CLEARED, "value": 20})
    topic = event.active_topic if active else event.topic
    payload = b"" if active and state == "cleared" else event.to_payload()
    return Message(topic, payload, at)


def test_open_alerts_and_alert_events(state: SystemState) -> None:
    state.apply_all([alert_message("raised", E0), alert_message("raised", E0, active=True)])
    assert [a.rule_id for a in state.alerts_of("poultry-fc5336-001")] == ["ammonia-high"]
    assert state.notable[-1].kind is EventKind.ALERT
    assert state.notable[-1].text == "ammonia-high warning: 30 ppm above 25"
    state.apply_all([alert_message("cleared", E0 + 1),
                     alert_message("cleared", E0 + 1, active=True)])  # fmt: skip
    assert state.open_alerts() == []
    assert state.notable[-1].text == "ammonia-high cleared at 20 ppm"
