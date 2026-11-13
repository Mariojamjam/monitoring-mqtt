import json
import uuid
from datetime import UTC, datetime

import pytest
import yaml
from pydantic import ValidationError
from test_worker import FakeDevice

from monitoring_mqtt.core.simulation import (
    ControlCommand,
    EpisodeKind,
    EpisodePhase,
    EpisodeSource,
    SimEvent,
    ack_topic,
    control_topic,
    sim_event_topic,
    worker_control_filter,
)
from monitoring_mqtt.simulator.behavior import (
    DeviceBehavior,
    Episode,
    EventFactory,
    build_behavior,
)
from monitoring_mqtt.simulator.behavior_config import BehaviorConfig, parse_duration
from monitoring_mqtt.simulator.catalog import DeviceType
from monitoring_mqtt.simulator.plane import SimulationPlane
from monitoring_mqtt.simulator.worker import SimulationWorker

PROBE = DeviceType.model_validate(
    {
        "name": "oxygen_probe", "domain": "aquaculture", "metric": "dissolved_oxygen",
        "unit": "mg/L", "site_prefix": "pond",
        "generator": {"kind": "mean_reverting", "mean": 6.5, "alpha": 0.3, "noise": 0.2,
                      "minimum": 2.0, "maximum": 12.0},
        "operating_range": {"minimum": 5.0, "maximum": 8.0},
        "anomaly": {"high": 10.0},
    }
)  # fmt: skip
DEVICE = "aquaculture-abc123-001"


def behavior(text: str, device_id: str = DEVICE, seed: int = 42) -> DeviceBehavior:
    return build_behavior(BehaviorConfig.model_validate(yaml.safe_load(text)), device_id,
                          PROBE, seed)  # fmt: skip


def timeline(b: DeviceBehavior, until: float, step: float = 1.0) -> list[tuple]:
    """(time, phase, kind, source) for every change, sampling like a worker would."""
    out = []
    t = 0.0
    while t <= until:
        for change in b.update(t):
            out.append((t, change.phase.value, change.episode.kind.value,
                        change.episode.source.value))  # fmt: skip
        t += step
    return out


# Configuration ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"), [("500ms", 0.5), ("30s", 30), ("2m", 120), ("1.5h", 5400), (" 10 s", 10)]
)
def test_durations_need_a_unit(text: str, seconds: float) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("value", [30, "30", "abc", "0s", "-5s", True])
def test_invalid_durations(value: object) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)


def test_config_round_trips_through_the_environment() -> None:
    config = BehaviorConfig.model_validate(
        yaml.safe_load("""
anomalies: {every: 2m}
instability: {every: 3m, lasts: 10s, kinds: [pause]}
faults: [{device: "poultry-*-002", at: 1m, action: stop}]
""")
    )
    assert config.anomalies is not None and config.anomalies.lasts == 30  # default
    assert BehaviorConfig.from_env(config.to_env()) == config
    assert BehaviorConfig.from_env("") == BehaviorConfig() and BehaviorConfig().to_env() == ""


@pytest.mark.parametrize(
    "text",
    [
        "anomalies: {lasts: 30s}",  # every is required
        "anomalies: {every: 2m, direction: up}",
        "instability: {every: 1m, kinds: [stop]}",  # stop is a fault, not instability
        "faults: [{device: 'Poultry/1', at: 1m, action: stop}]",
        "faults: [{device: x, at: 1m, action: explode}]",
        "faults: [{device: x, action: stop}]",  # when?
        "anomalie: {every: 2m}",  # typo
    ],
)
def test_invalid_behaviour(text: str) -> None:
    with pytest.raises(ValidationError):
        BehaviorConfig.model_validate(yaml.safe_load(text))


# Episodes -----------------------------------------------------------------------------


def test_no_behaviour_means_no_episodes() -> None:
    assert timeline(behavior("{}"), until=600) == []


def test_random_anomalies_are_reproducible_and_last_as_configured() -> None:
    text = "anomalies: {every: 1m, lasts: 20s}"
    first = timeline(behavior(text), until=900)
    assert first == timeline(behavior(text), until=900)
    assert first != timeline(behavior(text, seed=7), until=900)
    assert first != timeline(behavior(text, device_id="aquaculture-abc123-002"), until=900)
    starts = [t for t, phase, *_ in first if phase == "start"]
    ends = [t for t, phase, *_ in first if phase == "end"]
    assert 5 <= len(starts) <= 25  # about one every (60 + 20) s over 900 s
    assert all(19 <= end - start <= 21 for start, end in zip(starts, ends, strict=False))


def test_anomalies_use_the_directions_the_type_supports() -> None:
    b = behavior("anomalies: {every: 30s, lasts: 10s, direction: both}")
    directions = set()
    for t in range(0, 900):
        for change in b.update(float(t)):
            directions.add(change.episode.direction)
    assert directions == {"high"}  # the probe has no low target
    assert b.target_for(Episode(EpisodeKind.ANOMALY, EpisodeSource.FAULT, 0, 1, "high")) == 10.0


def test_only_limits_modes_to_some_device_types() -> None:
    assert timeline(behavior("anomalies: {every: 10s, only: [ammonia_sensor]}"), 300) == []


def test_scheduled_fault_matches_devices_by_pattern() -> None:
    text = "faults: [{device: 'aquaculture-*-001', at: 10s, lasts: 5s, action: stop}]"
    assert timeline(behavior(text), until=30) == [
        (10.0, "start", "stop", "fault"),
        (15.0, "end", "stop", "fault"),
    ]
    assert timeline(behavior(text, device_id="aquaculture-abc123-002"), until=30) == []


def test_fault_without_end_lasts_until_the_run_ends() -> None:
    b = behavior("faults: [{device: '*', at: 5s, action: pause}]")
    assert timeline(b, until=1000) == [(5.0, "start", "pause", "fault")]


def test_values_and_availability_are_independent() -> None:
    b = behavior("""
faults:
  - {device: '*', at: 10s, lasts: 20s, action: anomaly}
  - {device: '*', at: 15s, lasts: 5s, action: disconnect}
""")
    assert [(t, p, k) for t, p, k, _ in timeline(b, until=40)] == [
        (10.0, "start", "anomaly"),
        (15.0, "start", "disconnect"),
        (20.0, "end", "disconnect"),
        (30.0, "end", "anomaly"),
    ]


def test_live_command_overrides_and_resume_cancels() -> None:
    b = behavior("faults: [{device: '*', at: 0s, lasts: 100s, action: delay}]")
    assert b.update(0.0)[0].episode.kind is EpisodeKind.DELAY
    stop = ControlCommand(device_id=DEVICE, action="stop", lasts_seconds=30)
    assert b.submit(stop).accepted
    changes = b.update(10.0)
    assert [(c.phase.value, c.episode.kind.value) for c in changes] == [
        ("end", "delay"),
        ("start", "stop"),
    ]
    assert changes[1].episode.request_id == stop.request_id
    b.submit(ControlCommand(device_id=DEVICE, action="resume"))
    assert [(c.phase.value, c.episode.kind.value) for c in b.update(11.0)] == [("end", "stop")]
    assert b.update(12.0) == []  # the cancelled fault does not come back
    assert b.availability is None


def test_command_validation() -> None:
    b = behavior("{}")
    assert not b.check(ControlCommand(device_id=DEVICE, action="anomaly", direction="low")).accepted
    assert b.check(ControlCommand(device_id=DEVICE, action="anomaly", direction="high")).accepted


def test_events_carry_wall_clock_times() -> None:
    clock = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    factory = EventFactory("run-1", "abc123", clock=lambda: clock)
    b = behavior("faults: [{device: '*', at: 10s, lasts: 30s, action: anomaly}]")
    (change,) = b.update(10.0)
    event = factory.event(DEVICE, change, 10.0)
    assert (event.kind, event.phase, event.source, event.direction) == (
        EpisodeKind.ANOMALY, EpisodePhase.START, EpisodeSource.FAULT, "high"
    )  # fmt: skip
    assert event.until is not None and (event.until - clock).total_seconds() == 30
    assert event.topic == f"monitoring/sim/v1/devices/{DEVICE}"
    assert SimEvent.from_payload(event.to_payload()) == event


# Worker -------------------------------------------------------------------------------


def worker_with(text: str, devices: list[FakeDevice]) -> tuple[SimulationWorker, list[SimEvent]]:
    behaviors = [behavior(text, device_id=d.device_id) for d in devices]
    events: list[SimEvent] = []
    factory = EventFactory("run-1", "abc123")
    worker = SimulationWorker(
        "abc123", devices, 1.0, behaviors=behaviors, events=factory, on_event=events.append
    )
    for d in devices:
        d.start()
    return worker, events


def run_steps(worker: SimulationWorker, until: int) -> None:
    for t in range(until):
        for index in range(len(worker.devices)):
            worker.step(index, float(t))


def two_devices() -> list[FakeDevice]:
    return [FakeDevice("aquaculture-abc123-001"), FakeDevice("aquaculture-abc123-002")]


def test_stopping_one_device_leaves_its_neighbour_publishing() -> None:
    devices = two_devices()
    worker, events = worker_with(
        "faults: [{device: '*-001', at: 3s, lasts: 4s, action: stop}]", devices
    )
    run_steps(worker, 10)
    off, neighbour = devices
    # Off at 3-6 (nothing generated), back at 7: no gap in its sequence.
    assert off.published == [1, 2, 3, 4, 5, 6]
    assert neighbour.published == list(range(1, 11))
    assert off.connected  # back online after the fault
    assert [(e.phase.value, e.kind.value) for e in events] == [("start", "stop"), ("end", "stop")]


def test_pause_delay_and_disconnect() -> None:
    pause, delay, disconnect = (FakeDevice(f"aquaculture-abc123-00{i}") for i in (1, 2, 3))
    worker, _ = worker_with(
        """
faults:
  - {device: '*-001', at: 2s, lasts: 3s, action: pause}
  - {device: '*-002', at: 2s, lasts: 3s, action: delay}
  - {device: '*-003', at: 2s, lasts: 3s, action: disconnect}
""",
        [pause, delay, disconnect],
    )
    run_steps(worker, 3)
    assert delay.published == [1, 2]  # measurement 3 is held back
    assert not disconnect.connected
    run_steps_from(worker, 3, 8)
    assert pause.published == [1, 2, 3, 4, 5]  # silence, sequence continues
    assert delay.published == [1, 2, 3, 4, 5, 6, 7, 8]  # flushed in order at the end
    assert disconnect.published == [1, 2, 6, 7, 8]  # 3-5 generated and dropped: a gap
    assert disconnect.connected


def run_steps_from(worker: SimulationWorker, start: int, until: int) -> None:
    for t in range(start, until):
        for index in range(len(worker.devices)):
            worker.step(index, float(t))


def test_a_device_switched_back_on_waits_for_its_connection() -> None:
    class SlowToConnect(FakeDevice):
        def go_online(self) -> None:
            self.connects_at_step = 3  # the connection opens two publish times later

    device = SlowToConnect(DEVICE)
    worker, _ = worker_with("faults: [{device: '*', at: 1s, lasts: 1s, action: stop}]", [device])
    worker.step(0, 0.0)  # seq 1
    worker.step(0, 1.0)  # stop: off
    worker.step(0, 2.0)  # stop ends: reconnecting
    worker.step(0, 3.0)  # still not connected: generates nothing
    device.connected = True
    worker.step(0, 4.0)  # connected: seq 2, no gap
    assert device.published == [1, 2]


def test_reconnection_wait_is_bounded() -> None:
    class NeverConnects(FakeDevice):
        def go_online(self) -> None:
            pass  # stays disconnected (a real broker outage)

    device = NeverConnects(DEVICE)
    worker, _ = worker_with("faults: [{device: '*', at: 1s, lasts: 1s, action: stop}]", [device])
    worker.connect_timeout = 3.0
    for t in range(8):
        worker.step(0, float(t))
    assert device.sequence > 1  # after the timeout it generates again (and cannot send)


def test_anomaly_steers_the_generator() -> None:
    (device,) = devices = [FakeDevice(DEVICE)]
    worker, _ = worker_with("faults: [{device: '*', at: 1s, lasts: 2s, action: anomaly}]", devices)
    worker.step(0, 1.0)
    assert device.generator.target == 10.0
    worker.step(0, 3.0)
    assert device.generator.target is None


def test_worker_routes_live_commands_to_its_devices() -> None:
    devices = two_devices()
    worker, _ = worker_with("{}", devices)
    command = ControlCommand(device_id="aquaculture-abc123-002", action="pause")
    assert worker.submit("aquaculture-abc123-002", command) == (
        True, "applies at the device's next publish time"
    )  # fmt: skip
    accepted, detail = worker.submit("poultry-zzz-001", command)
    assert not accepted and "not hosted" in detail
    worker.step(1, 0.0)
    assert devices[1].published == []


# Control plane ------------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.on_message = None

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        self.published.append((topic, payload))


def test_plane_acknowledges_every_command() -> None:
    from monitoring_mqtt.core.config import MqttSettings

    client = FakeClient()
    received = []

    def on_command(device_id: str, command: ControlCommand) -> tuple[bool, str]:
        received.append((device_id, command.action))
        return True, "ok"

    plane = SimulationPlane("abc123", MqttSettings(), on_command, client=client)  # type: ignore[arg-type]
    command = ControlCommand(device_id=DEVICE, action="stop", lasts_seconds=10)
    ack = plane.handle(control_topic("abc123", DEVICE), command.to_payload())
    assert ack is not None and ack.accepted and received == [(DEVICE, "stop")]
    assert client.published[-1][0] == ack_topic(command.request_id)

    # Payload for another device than the topic says: rejected, and the handler is not called.
    other = ControlCommand(device_id="aquaculture-abc123-009", action="stop")
    ack = plane.handle(control_topic("abc123", DEVICE), other.to_payload())
    assert ack is not None and not ack.accepted and len(received) == 1
    assert plane.handle(control_topic("abc123", DEVICE), b"not json") is None


def test_simulation_topics() -> None:
    request = uuid.uuid4()
    assert sim_event_topic(DEVICE) == f"monitoring/sim/v1/devices/{DEVICE}"
    assert control_topic("abc123", DEVICE) == (
        f"monitoring/control/v1/workers/abc123/devices/{DEVICE}"
    )
    assert worker_control_filter("abc123") == "monitoring/control/v1/workers/abc123/devices/+"
    assert ack_topic(request) == f"monitoring/control/v1/acks/{request}"
    assert json.loads(ControlCommand(device_id=DEVICE, action="resume").to_payload())["action"] == (
        "resume"
    )
