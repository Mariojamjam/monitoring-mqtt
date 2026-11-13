"""Recovery from a broker restart and from a network cut (destructive: runs last)."""

import time

from harness import NETWORK, Observer, Stack, gaps

INTERVAL = 0.5  # publish_interval_seconds in experiment.yaml
MAX_OUTAGE = 10.0  # broker restart plus reconnection backoff (1-2 s), with a wide margin


def test_everyone_recovers_from_a_broker_restart(stack: Stack, observer: Observer) -> None:
    connects = observer.connects
    devices = observer.active()
    received = {k: s.counters.received for k, s in observer.monitors().items() if s.counters}
    t0 = time.time()
    assert stack.cli("stack", "restart", "broker").returncode == 0
    observer.wait_until(lambda: observer.connects > connects, "observer reconnected")
    after = time.time()
    # Every device publishes again; only what fell in the outage was lost (the device knew
    # it was disconnected and dropped it, see docs/failures.md).
    for device in devices:
        observer.wait_until(lambda d=device: len(observer.samples(d, since=after)) >= 2,
                            f"{device} publishes again")  # fmt: skip
        lost = gaps(observer.samples(device, since=t0 - 1))
        assert lost <= MAX_OUTAGE / INTERVAL, f"{device} lost {lost}"
    # Monitors subscribed again (clean sessions) and keep receiving.
    observer.wait_until(
        lambda: all(
            (s := observer.monitors().get(k)) is not None
            and s.state == "online"
            and s.counters is not None
            and s.counters.received > n
            for k, n in received.items()
        ),
        "every monitor receives again",
    )
    observer.wait_until(lambda: len(observer.present_claims()) == 4, "workers claimed again")


def test_a_cut_worker_is_detected_by_keepalive(stack: Stack, observer: Observer) -> None:
    device = observer.active("cattle")[0]
    worker_id = device.split("-")[1]
    container = stack.container("device-cattle")
    t0 = time.time()
    assert stack.docker("network", "disconnect", NETWORK, container).returncode == 0
    try:
        # Nothing tells the broker; keepalive 5 s: Last Will within 1.5 x 5 s (+ margin).
        observer.wait_until(lambda: worker_id not in observer.present_claims(),
                            "Last Will after the keepalive timeout", 15)  # fmt: skip
        detected = time.time() - t0
        assert detected >= 3  # not immediate: the broker had to notice the silence
    finally:
        stack.docker("network", "connect", NETWORK, container)
    observer.wait_until(lambda: worker_id in observer.present_claims(), "worker back", 30)
    observer.wait_until(lambda: observer.samples(device, since=time.time() - 1), "telemetry")
