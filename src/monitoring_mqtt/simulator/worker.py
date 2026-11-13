"""SimulationWorker: hosts M virtual devices in one process.

The worker aggregates devices; it does not share their identities. Each device keeps its own
MQTT client and connection, so a device failure affects only that device, while a worker
(container) failure affects every device it hosts.

Each device may have a behaviour (``behavior.py``): at every publish time the worker asks it
which episodes are in effect and applies them to that device only. Its neighbours keep
publishing normally.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Protocol

from monitoring_mqtt.core.logging import kv
from monitoring_mqtt.core.simulation import EpisodeKind, EpisodePhase, SimEvent
from monitoring_mqtt.simulator.behavior import Change, DeviceBehavior, EventFactory

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10.0
CONNECT_POLL_SECONDS = 0.05
# A delay that never ends must not exhaust memory: keep at most this many measurements.
MAX_DELAYED_MEASUREMENTS = 10_000

SILENT = (EpisodeKind.PAUSE, EpisodeKind.STOP)
OFFLINE = (EpisodeKind.DISCONNECT, EpisodeKind.STOP)


class Generator(Protocol):
    def steer(self, target: float | None) -> None: ...


class Device(Protocol):
    device_id: str
    sequence: int
    generator: Generator

    @property
    def connected(self) -> bool: ...

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def go_offline(self) -> None: ...
    def go_online(self) -> None: ...
    def next_measurement(self) -> object: ...
    def publish(self, measurement) -> object: ...


def initial_offsets(count: int, interval: float) -> list[float]:
    """Spread first publications evenly over one interval to avoid bursts."""
    return [interval * i / count for i in range(count)] if count else []


class SimulationWorker:
    def __init__(
        self,
        worker_id: str,
        devices: Sequence[Device],
        interval: float,
        clock: Callable[[], float] = time.monotonic,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        behaviors: Sequence[DeviceBehavior | None] | None = None,
        events: EventFactory | None = None,
        on_event: Callable[[SimEvent], None] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.worker_id = worker_id
        self.devices = list(devices)
        self.interval = interval
        self._clock = clock
        self.connect_timeout = connect_timeout
        self.behaviors: list[DeviceBehavior | None] = (
            list(behaviors) if behaviors is not None else [None] * len(self.devices)
        )
        if len(self.behaviors) != len(self.devices):
            raise ValueError("one behaviour (or None) per device")
        self._events = events
        self._on_event = on_event
        self._delayed: list[deque] = [deque(maxlen=MAX_DELAYED_MEASUREMENTS) for _ in self.devices]
        self._started_at = 0.0
        # Devices coming back after a simulated stop or disconnection: until when they may
        # wait for their connection before generating again.
        self._reconnecting: dict[int, float] = {}

    def run(self, stop: threading.Event) -> None:
        """Publish from every device at its own fixed rate until ``stop`` is set."""
        for device in self.devices:
            device.start()
        connected = self._wait_for_connections(stop)
        logger.info(
            "worker started %s",
            kv(worker_id=self.worker_id, devices=len(self.devices), connected=connected),
        )

        start = self._started_at = self._clock()
        due = [start + offset for offset in initial_offsets(len(self.devices), self.interval)]
        try:
            while self.devices and not stop.is_set():
                index = min(range(len(due)), key=due.__getitem__)
                if stop.wait(max(0.0, due[index] - self._clock())):
                    break
                self.step(index, self._clock() - start)
                # Fixed-rate schedule: no drift accumulates from publish duration.
                due[index] += self.interval
            if not self.devices:
                stop.wait()
        finally:
            self.shutdown()

    def _wait_for_connections(self, stop: threading.Event) -> int:
        """Wait (bounded) until devices connect, so the first measurements are not dropped.

        If the broker is unavailable, publishing starts anyway after the timeout and
        measurements are dropped and logged until each device connects.
        """
        deadline = self._clock() + self.connect_timeout
        while not stop.is_set():
            connected = sum(1 for d in self.devices if d.connected)
            if connected == len(self.devices) or self._clock() >= deadline:
                return connected
            stop.wait(CONNECT_POLL_SECONDS)
        return sum(1 for d in self.devices if d.connected)

    def step(self, index: int, elapsed: float) -> None:
        """One publish time of one device: apply its episodes, then generate and publish."""
        device = self.devices[index]
        try:
            behavior = self.behaviors[index]
            mode = None
            if behavior is not None:
                for change in behavior.update(elapsed):
                    self._apply(index, behavior, change, elapsed)
                    if change.phase is EpisodePhase.END and change.episode.kind in OFFLINE:
                        self._reconnecting[index] = elapsed + self.connect_timeout
                mode = behavior.availability
            if mode in SILENT or self._still_reconnecting(index, mode, elapsed):
                return
            measurement = device.next_measurement()
            if mode is EpisodeKind.DELAY:
                self._delayed[index].append(measurement)
            elif mode is EpisodeKind.DISCONNECT:
                logger.info(
                    "measurement dropped, simulated disconnection %s",
                    kv(device_id=device.device_id, seq=device.sequence),
                )
            else:
                device.publish(measurement)
        except Exception:
            # One misbehaving device must not stop the others.
            logger.exception("device step failed %s", kv(device_id=device.device_id))

    def _still_reconnecting(self, index: int, mode: EpisodeKind | None, elapsed: float) -> bool:
        """A device switched back on generates nothing until its connection is open.

        Reconnecting can take longer than one publish interval; without this, the first
        measurements after a stop would be generated and lost. If the connection does not
        come back within the connect timeout (a real broker outage), the device publishes
        again and drops what it cannot send, like any disconnected device.
        """
        deadline = self._reconnecting.get(index)
        if deadline is None:
            return False
        if mode in OFFLINE:
            return False  # another offline episode took over; it generates or not by itself
        if self.devices[index].connected or elapsed >= deadline:
            del self._reconnecting[index]
            return False
        return True

    def _apply(self, index: int, behavior: DeviceBehavior, change: Change, t: float) -> None:
        device = self.devices[index]
        episode = change.episode
        starting = change.phase is EpisodePhase.START
        if episode.kind is EpisodeKind.ANOMALY:
            device.generator.steer(behavior.target_for(episode) if starting else None)
        elif episode.kind in OFFLINE:
            device.go_offline() if starting else device.go_online()
        elif episode.kind is EpisodeKind.DELAY and not starting:
            delayed = self._delayed[index]
            while delayed:
                device.publish(delayed.popleft())
        logger.info(
            "episode %s %s",
            change.phase.value,
            kv(
                device_id=device.device_id,
                kind=episode.kind.value,
                source=episode.source.value,
                direction=episode.direction,
                lasts_s=None if episode.end is None else round(episode.end - episode.start, 3),
            ),
        )
        if self._events is not None and self._on_event is not None:
            self._on_event(self._events.event(device.device_id, change, t))

    def attach_events(self, on_event: Callable[[SimEvent], None]) -> None:
        """Where ground truth events go (set before :meth:`run`)."""
        self._on_event = on_event

    def submit(self, device_id: str, command) -> tuple[bool, str]:
        """Hand a live command to one of this worker's devices (control-plane thread)."""
        for device, behavior in zip(self.devices, self.behaviors, strict=True):
            if device.device_id == device_id:
                if behavior is None:
                    return False, "device has no behaviour"
                result = behavior.submit(command)
                return result.accepted, result.detail
        return False, f"device {device_id} is not hosted by worker {self.worker_id}"

    def shutdown(self) -> None:
        for index, device in enumerate(self.devices):
            lost = len(self._delayed[index])
            if lost:
                logger.warning(
                    "delayed measurements not sent %s", kv(device_id=device.device_id, count=lost)
                )
            try:
                device.stop()
            except Exception:
                logger.exception("device stop failed %s", kv(device_id=device.device_id))
        logger.info(
            "worker stopped %s",
            kv(
                worker_id=self.worker_id,
                generated=sum(d.sequence for d in self.devices),
            ),
        )
