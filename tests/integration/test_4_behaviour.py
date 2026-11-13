"""Simulated episodes: ground truth, values, and live commands on single devices."""

import itertools
from datetime import UTC, datetime

import pytest
from harness import Observer, Stack, gaps, sequences

from monitoring_mqtt.core.simulation import SimEvent

RUMEN_PH_MAX = 6.8  # operating_range of rumen_ph_bolus in config/devices.yaml


def finished(observer: Observer, device: str, kind: str, source: str, after: float):
    """The (start, end) ground truth events of an episode that ended after ``after``."""

    def pair() -> tuple[SimEvent, SimEvent] | None:
        events = [e for e in observer.events(device) if e.kind == kind and e.source == source]
        ends = [e for e in events if e.phase == "end" and e.at.timestamp() > after]
        starts = [e for e in events if e.phase == "start"]
        if not ends or not starts:
            return None
        end = ends[-1]
        start = max((s for s in starts if s.at <= end.at), key=lambda s: s.at, default=None)
        return (start, end) if start else None

    return pair


def history(observer: Observer, device: str):
    """Failure message: the ground truth events seen for ``device``."""
    return lambda: (
        f"episode of {device}; events: "
        + "; ".join(
            f"{e.kind.value} {e.phase.value} {e.source.value} {e.at:%H:%M:%S}"
            for e in observer.events(device)
        )
    )


def manual_episode(observer: Observer, device: str, kind: str, t0: float):
    """Wait for a live command's episode on ``device`` to start and end."""
    return observer.wait_until(finished(observer, device, kind, "manual", t0),
                               history(observer, device))  # fmt: skip


def now() -> float:
    return datetime.now(UTC).timestamp()


def test_scheduled_anomaly_matches_its_ground_truth(observer: Observer) -> None:
    (device,) = observer.active("cattle")
    start, end = observer.wait_until(
        finished(observer, device, "anomaly", "fault", after=0), history(observer, device), 60
    )
    assert start.direction == "high" and start.until is not None
    during = [s.value for s in observer.samples(device, since=start.at.timestamp())
              if s.received_at <= end.at.timestamp()]  # fmt: skip
    assert max(during) > RUMEN_PH_MAX, during  # left the normal band


@pytest.fixture
def neighbours(observer: Observer) -> tuple[str, str]:
    first, second = observer.active("aquaculture")  # two devices in one worker container
    assert first.split("-")[1] == second.split("-")[1]
    return first, second


def test_stop_one_device_only(stack: Stack, observer: Observer, neighbours) -> None:
    target, neighbour = neighbours
    t0 = now()
    result = stack.cli("device", "stop", target, "--for", "3s")
    assert result.returncode == 0, result.stdout + result.stderr
    start, end = manual_episode(observer, target, "stop", t0)
    observer.wait_until(lambda: observer.samples(target, since=end.at.timestamp() + 1),
                        "target publishes again")  # fmt: skip
    window = (start.at.timestamp() + 0.2, end.at.timestamp())
    silent = [s for s in observer.samples(target) if window[0] < s.received_at < window[1]]
    assert silent == []
    assert gaps(observer.samples(target, since=t0)) == 0  # off: nothing generated, no gap
    busy = [s for s in observer.samples(neighbour) if window[0] < s.received_at < window[1]]
    assert len(busy) >= 4  # its neighbour in the same container kept publishing


def test_disconnect_loses_measurements(stack: Stack, observer: Observer, neighbours) -> None:
    target, _ = neighbours
    t0 = now()
    assert stack.cli("device", "disconnect", target, "--for", "2s").returncode == 0
    manual_episode(observer, target, "disconnect", t0)
    observer.wait_until(lambda: len(observer.samples(target, since=now() - 1)) > 1,
                        "target publishes again")  # fmt: skip
    assert gaps(observer.samples(target, since=t0)) >= 2, sequences(observer.samples(target, t0))


def test_delay_delivers_late_but_complete(stack: Stack, observer: Observer, neighbours) -> None:
    _, target = neighbours
    t0 = now()
    assert stack.cli("device", "delay", target, "--for", "3s").returncode == 0
    manual_episode(observer, target, "delay", t0)
    observer.wait_until(lambda: len(observer.samples(target, since=now() - 1)) > 1, "after")
    samples = observer.samples(target, since=t0)
    assert gaps(samples) == 0
    assert max(s.latency for s in samples) >= 2.0  # held back, then sent together


def test_pause_is_silence_without_gaps(stack: Stack, observer: Observer, neighbours) -> None:
    _, target = neighbours
    t0 = now()
    assert stack.cli("device", "pause", target, "--for", "2s").returncode == 0
    manual_episode(observer, target, "pause", t0)
    observer.wait_until(lambda: len(observer.samples(target, since=now() - 1)) > 1, "after")
    samples = observer.samples(target, since=t0)
    assert gaps(samples) == 0
    arrivals = [s.received_at for s in samples]
    assert max(b - a for a, b in itertools.pairwise(arrivals)) >= 1.5


def test_commands_that_cannot_run(stack: Stack, observer: Observer) -> None:
    poultry = observer.active("poultry")[0]
    rejected = stack.cli("device", "anomaly", poultry, "--direction", "low")
    assert rejected.returncode == 1 and "no low anomaly" in rejected.stderr
    assert stack.cli("device", "stop", "poultry-nobody-001").returncode == 3
    assert stack.cli("device", "stop", "not-a-device").returncode == 2
