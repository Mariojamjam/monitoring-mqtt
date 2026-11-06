"""SimulationWorker: hosts M virtual devices in one process.

The worker aggregates devices; it does not share their identities. Each device keeps its own
MQTT client and connection, so a device failure affects only that device, while a worker
(container) failure affects every device it hosts.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from monitoring_mqtt.core.logging import kv

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10.0
CONNECT_POLL_SECONDS = 0.05


class Device(Protocol):
    device_id: str
    sequence: int

    @property
    def connected(self) -> bool: ...

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def publish_next(self) -> object: ...


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
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.worker_id = worker_id
        self.devices = list(devices)
        self.interval = interval
        self._clock = clock
        self.connect_timeout = connect_timeout

    def run(self, stop: threading.Event) -> None:
        """Publish from every device at its own fixed rate until ``stop`` is set."""
        for device in self.devices:
            device.start()
        connected = self._wait_for_connections(stop)
        logger.info(
            "worker started %s",
            kv(worker_id=self.worker_id, devices=len(self.devices), connected=connected),
        )

        start = self._clock()
        due = [start + offset for offset in initial_offsets(len(self.devices), self.interval)]
        try:
            while self.devices and not stop.is_set():
                index = min(range(len(due)), key=due.__getitem__)
                if stop.wait(max(0.0, due[index] - self._clock())):
                    break
                self._publish(self.devices[index])
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

    def _publish(self, device: Device) -> None:
        try:
            device.publish_next()
        except Exception:
            # One misbehaving device must not stop the others.
            logger.exception("device publish failed %s", kv(device_id=device.device_id))

    def shutdown(self) -> None:
        for device in self.devices:
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
