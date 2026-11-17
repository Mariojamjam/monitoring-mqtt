from monitoring_mqtt.consumers.monitor import MonitorStats
from monitoring_mqtt.core.status import (
    MonitorState,
    PublisherCounters,
    WorkerStatus,
    worker_status_topic,
)
from monitoring_mqtt.simulator.status import WorkerStatusReporter


class FakeInfo:
    def wait_for_publish(self, timeout: float) -> None:
        pass


class FakeClient:
    def __init__(self) -> None:
        self.connected = True
        self.will = None
        self.published: list[tuple[str, bytes | None, bool]] = []

    def is_connected(self) -> bool:
        return self.connected

    def will_set(self, topic, payload, qos, retain) -> None:
        self.will = (topic, payload, retain)

    def publish(self, topic, payload, qos=0, retain=False) -> FakeInfo:
        self.published.append((topic, payload, retain))
        return FakeInfo()


def reporter(client: FakeClient) -> WorkerStatusReporter:
    counters = [
        PublisherCounters(
            device_id="poultry-abc123-001", generated=5, published=4, acked=4, failed=1
        )
    ]
    return WorkerStatusReporter(
        worker_id="abc123",
        run_id="r1",
        hostname="h",
        node="pc",
        qos=1,
        counters=lambda: counters,
        client=client,
    )  # type: ignore[arg-type]


def test_worker_status_lifecycle() -> None:
    client = FakeClient()
    r = reporter(client)
    r.install_will()
    topic, payload, retain = client.will
    assert topic == worker_status_topic("abc123") and retain
    assert WorkerStatus.from_payload(payload).state is MonitorState.OFFLINE
    r.publish()
    status = WorkerStatus.from_payload(client.published[-1][1])
    assert status.total("published") == 4 and status.run_id == "r1" and client.published[-1][2]
    r.stop()
    assert client.published[-1] == (topic, None, True)  # retained status removed
    assert WorkerStatus.from_payload(client.published[-2][1]).state is MonitorState.STOPPED


def test_monitor_counts_duplicate_deliveries(measurement) -> None:
    stats = MonitorStats()
    m = measurement()
    for _ in range(3):  # QoS 1 may deliver the same event more than once
        stats.record(m.topic, m, 1.0, m.timestamp)
    stats.record(m.topic, measurement(sequence=2), 1.0, m.timestamp)
    counters = stats.snapshot()
    assert (counters.received, counters.duplicates) == (4, 2)
