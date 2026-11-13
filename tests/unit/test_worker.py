import threading

from monitoring_mqtt.simulator.worker import SimulationWorker, initial_offsets


class FakeGenerator:
    def __init__(self) -> None:
        self.target: float | None = None

    def steer(self, target: float | None) -> None:
        self.target = target


class FakeDevice:
    def __init__(self, device_id: str, fail: bool = False) -> None:
        self.device_id = device_id
        self.sequence = 0
        self.connected = False
        self.fail = fail
        self.started = self.stopped = False
        self.generator = FakeGenerator()
        self.published: list[int] = []

    def start(self) -> None:
        self.started = True
        self.connected = True

    def stop(self) -> None:
        self.stopped = True

    def go_offline(self) -> None:
        self.connected = False

    def go_online(self) -> None:
        self.connected = True

    def next_measurement(self) -> int:
        self.sequence += 1
        if self.fail:
            raise RuntimeError("boom")
        return self.sequence

    def publish(self, measurement: int) -> int:
        self.published.append(measurement)
        return measurement


def run_until(worker: SimulationWorker, devices: list[FakeDevice], publishes: int) -> None:
    stop = threading.Event()

    def watcher() -> None:
        while sum(d.sequence for d in devices) < publishes:
            stop.wait(0.001)
        stop.set()

    threading.Thread(target=watcher, daemon=True).start()
    worker.run(stop)


def test_initial_offsets_spread_over_interval() -> None:
    assert initial_offsets(4, 2.0) == [0.0, 0.5, 1.0, 1.5]
    assert initial_offsets(0, 2.0) == []


def test_all_devices_publish_and_stop_cleanly() -> None:
    devices = [FakeDevice(f"d{i}") for i in range(3)]
    run_until(SimulationWorker("w", devices, interval=0.01), devices, publishes=30)
    assert all(d.started and d.stopped for d in devices)
    assert all(d.sequence >= 5 for d in devices)


def test_failing_device_does_not_stop_others() -> None:
    devices = [FakeDevice("bad", fail=True), FakeDevice("good")]
    run_until(SimulationWorker("w", devices, interval=0.01), devices, publishes=20)
    assert devices[1].sequence >= 5


def test_waits_for_connections_but_not_forever() -> None:
    class NeverConnects(FakeDevice):
        def start(self) -> None:
            self.started = True

    devices = [NeverConnects("offline")]
    worker = SimulationWorker("w", devices, interval=0.01, connect_timeout=0.05)
    run_until(worker, devices, publishes=2)
    assert devices[0].sequence >= 2
