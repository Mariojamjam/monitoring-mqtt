"""Episodes of each device: when it is anomalous, paused, delayed, disconnected or off.

A device has two independent *families* of episodes:

- values: an anomaly steers its generator outside the operating range;
- availability: pause, delay, disconnect or stop change how it publishes.

Episodes come from four sources. When several are active in the same family, the most
specific wins: a live command, then a scheduled fault, then a random episode.

Times are seconds since the worker started. Random episodes are drawn from a stream seeded
by the experiment seed and the device id, so a run repeats exactly; the gaps between them
follow an exponential distribution whose mean is ``every``.

The worker calls :meth:`DeviceBehavior.update` at each of the device's publish times and
applies the returned changes. Commands from the control plane arrive on another thread and
take effect at that next update.
"""

from __future__ import annotations

import logging
import random
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from monitoring_mqtt.core.logging import kv
from monitoring_mqtt.core.simulation import (
    RESUME,
    ControlCommand,
    EpisodeKind,
    EpisodePhase,
    EpisodeSource,
    SimEvent,
)
from monitoring_mqtt.simulator.behavior_config import (
    AnomaliesConfig,
    BehaviorConfig,
    Direction,
    InstabilityConfig,
)
from monitoring_mqtt.simulator.catalog import AnomalyTargets, DeviceType
from monitoring_mqtt.simulator.generators.base import seeded_rng

logger = logging.getLogger(__name__)

PRIORITY = {
    EpisodeSource.MANUAL: 3,
    EpisodeSource.FAULT: 2,
    EpisodeSource.ANOMALIES: 1,
    EpisodeSource.INSTABILITY: 1,
}


@dataclass(frozen=True)
class Episode:
    kind: EpisodeKind
    source: EpisodeSource
    start: float
    end: float | None  # None: until the end of the run (or a `resume`)
    direction: str | None = None  # anomalies: "high" or "low"
    request_id: uuid.UUID | None = None
    serial: int = 0  # tells apart identical episodes from different commands

    def active(self, t: float) -> bool:
        return self.start <= t and (self.end is None or t < self.end)


@dataclass(frozen=True)
class Change:
    phase: EpisodePhase
    episode: Episode


class RandomEpisodes:
    """An endless, reproducible series of episodes from one source."""

    def __init__(
        self,
        rng: random.Random,
        every: float,
        lasts: float,
        source: EpisodeSource,
        choose: Callable[[random.Random], tuple[EpisodeKind, str | None]],
    ) -> None:
        self._rng = rng
        self._every = every
        self._lasts = lasts
        self._source = source
        self._choose = choose
        self._current = self._make(after=0.0)

    def _make(self, after: float) -> Episode:
        start = after + self._rng.expovariate(1.0 / self._every)
        kind, direction = self._choose(self._rng)
        return Episode(kind, self._source, start, start + self._lasts, direction)

    def at(self, t: float) -> Episode | None:
        while self._current.end is not None and self._current.end <= t:
            self._current = self._make(after=self._current.end)
        return self._current if self._current.active(t) else None


def anomaly_directions(targets: AnomalyTargets, wanted: Direction | str) -> list[str]:
    """Directions an anomaly can take for a device type (``both`` = all it supports)."""
    available = targets.directions
    if wanted == Direction.BOTH:
        return available
    return [d for d in available if d == wanted]


def _anomaly_choice(directions: Sequence[str]):
    def choose(rng: random.Random) -> tuple[EpisodeKind, str | None]:
        return EpisodeKind.ANOMALY, rng.choice(list(directions))

    return choose


def _instability_choice(kinds: Sequence[EpisodeKind]):
    def choose(rng: random.Random) -> tuple[EpisodeKind, str | None]:
        return rng.choice(list(kinds)), None

    return choose


@dataclass
class _State:
    value: Episode | None = None
    availability: Episode | None = None


@dataclass
class CommandResult:
    accepted: bool
    detail: str


class DeviceBehavior:
    """Decides, at each publish time, which episodes are in effect for one device."""

    def __init__(
        self,
        device_id: str,
        anomaly_targets: AnomalyTargets | None = None,
        random_sources: Iterable[RandomEpisodes] = (),
        scheduled: Iterable[Episode] = (),
    ) -> None:
        self.device_id = device_id
        self.anomaly_targets = anomaly_targets
        self._random = list(random_sources)
        self._scheduled = sorted(scheduled, key=lambda e: e.start)
        self._manual: list[Episode] = []
        self._cancelled: set[Episode] = set()
        self._pending: list[ControlCommand] = []
        self._lock = threading.Lock()
        self._serial = 0
        self.state = _State()

    @property
    def availability(self) -> EpisodeKind | None:
        return self.state.availability.kind if self.state.availability else None

    def target_for(self, episode: Episode) -> float | None:
        if self.anomaly_targets is None or episode.direction is None:
            return None
        return self.anomaly_targets.target(episode.direction)

    # Commands (control-plane thread) -------------------------------------------------

    def check(self, command: ControlCommand) -> CommandResult:
        """Whether this device can run ``command``, without applying it."""
        if command.action == EpisodeKind.ANOMALY:
            if self.anomaly_targets is None:
                return CommandResult(False, "this device type has no anomaly targets")
            if command.direction not in self.anomaly_targets.directions:
                return CommandResult(False, f"this device type has no {command.direction} anomaly")
        return CommandResult(True, "applies at the device's next publish time")

    def submit(self, command: ControlCommand) -> CommandResult:
        result = self.check(command)
        if result.accepted:
            with self._lock:
                self._pending.append(command)
        return result

    # Worker thread ------------------------------------------------------------------

    def update(self, t: float) -> list[Change]:
        """Apply commands, expire episodes and return what started or ended at ``t``."""
        self._apply_commands(t)
        self._manual = [e for e in self._manual if e.end is None or e.end > t]
        new = _State(
            value=self._effective(t, values=True),
            availability=self._effective(t, values=False),
        )
        changes: list[Change] = []
        starts: list[Change] = []
        for old_episode, new_episode in (
            (self.state.value, new.value),
            (self.state.availability, new.availability),
        ):
            if old_episode != new_episode:
                if old_episode is not None:
                    changes.append(Change(EpisodePhase.END, old_episode))
                if new_episode is not None:
                    starts.append(Change(EpisodePhase.START, new_episode))
        self.state = new
        return changes + starts  # ends first: e.g. flush a delay before disconnecting

    def _apply_commands(self, t: float) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
        for command in pending:
            if command.action == RESUME:
                # Back to normal now: end every active episode, including those that a
                # higher-priority one was hiding, so none of them takes over afterwards.
                self._cancelled.update(self._candidates(t))
                self._manual = []
                continue
            self._serial += 1
            kind = EpisodeKind(command.action)
            end = t + command.lasts_seconds if command.lasts_seconds else None
            direction = command.direction if kind is EpisodeKind.ANOMALY else None
            family = kind.affects_values
            # A new command replaces the previous manual episode of the same family.
            self._manual = [e for e in self._manual if e.kind.affects_values != family]
            self._manual.append(
                Episode(
                    kind, EpisodeSource.MANUAL, t, end, direction, command.request_id, self._serial
                )
            )

    def _candidates(self, t: float) -> list[Episode]:
        found = [e for e in self._manual if e.active(t)]
        found += [e for e in self._scheduled if e.active(t)]
        found += [e for e in (src.at(t) for src in self._random) if e is not None]
        return [e for e in found if e not in self._cancelled]

    def _effective(self, t: float, *, values: bool) -> Episode | None:
        family = [e for e in self._candidates(t) if e.kind.affects_values == values]
        if not family:
            return None
        return max(family, key=lambda e: (PRIORITY[e.source], e.start))


# Building behaviours from the experiment ---------------------------------------------


def _wanted(section: AnomaliesConfig | InstabilityConfig | None, type_name: str) -> bool:
    return section is not None and (not section.only or type_name in section.only)


def build_behavior(
    config: BehaviorConfig, device_id: str, device_type: DeviceType, seed: int
) -> DeviceBehavior:
    sources = []
    targets = device_type.anomaly
    anomalies = config.anomalies
    if anomalies and targets and _wanted(anomalies, device_type.name):
        directions = anomaly_directions(targets, anomalies.direction)
        if directions:
            sources.append(
                RandomEpisodes(
                    seeded_rng(seed, device_id, "anomalies"),
                    anomalies.every,
                    anomalies.lasts,
                    EpisodeSource.ANOMALIES,
                    _anomaly_choice(directions),
                )
            )
    instability = config.instability
    if instability and _wanted(instability, device_type.name):
        sources.append(
            RandomEpisodes(
                seeded_rng(seed, device_id, "instability"),
                instability.every,
                instability.lasts,
                EpisodeSource.INSTABILITY,
                _instability_choice(instability.kinds),
            )
        )
    scheduled = []
    for fault in config.faults:
        if not fault.matches(device_id):
            continue
        direction = None
        if fault.action is EpisodeKind.ANOMALY:
            if targets is None or not targets.directions:
                logger.warning("fault ignored, no anomaly targets %s", kv(device_id=device_id))
                continue
            direction = (
                fault.direction
                if fault.direction in targets.directions
                else (targets.directions[0])
            )
        end = fault.at + fault.lasts if fault.lasts else None
        scheduled.append(Episode(fault.action, EpisodeSource.FAULT, fault.at, end, direction))
    return DeviceBehavior(device_id, targets, sources, scheduled)


@dataclass
class EventFactory:
    """Turns episode changes into ground truth events with wall-clock times."""

    run_id: str
    worker_id: str
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def event(self, device_id: str, change: Change, t: float) -> SimEvent:
        episode = change.episode
        now = self.clock()
        until = None
        if change.phase is EpisodePhase.START and episode.end is not None:
            until = now + timedelta(seconds=max(0.0, episode.end - t))
        return SimEvent(
            run_id=self.run_id,
            worker_id=self.worker_id,
            device_id=device_id,
            kind=episode.kind,
            phase=change.phase,
            source=episode.source,
            direction=episode.direction,  # type: ignore[arg-type]
            until=until,
            request_id=episode.request_id,
            at=now,
        )
