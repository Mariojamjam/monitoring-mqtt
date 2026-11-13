"""Publishers and subscribers only know the broker: stopping one side never stops the other."""

import time

from harness import Observer, Stack, gaps, sequences

MONITORS = ["monitor-aquaculture", "monitor-poultry", "monitor-cattle", "monitor-all"]


def test_devices_publish_with_nobody_listening(stack: Stack, observer: Observer) -> None:
    assert stack.compose("stop", *MONITORS).returncode == 0
    try:
        # Clean stop: every monitor removes its status (empty retained payload).
        observer.wait_until(
            lambda: not observer.monitors(),
            lambda: f"every monitor status removed; left: {sorted(observer.monitors())}",
        )
        start = time.time()
        time.sleep(3)
        for device in observer.active():
            samples = observer.samples(device, since=start)
            assert len(samples) >= 4, f"{device} stopped publishing"
            assert gaps(samples) == 0, f"{device}: {sequences(samples)}"
    finally:
        stack.start(*MONITORS)
    observer.wait_until(
        lambda: (
            len(observer.monitors()) == 5
            and all(s.state == "online" for s in observer.monitors().values())
        ),
        lambda: f"monitors back online: {[(k, s.state) for k, s in observer.monitors().items()]}",
    )


def test_a_crashed_monitor_is_reported_offline(stack: Stack, observer: Observer) -> None:
    client_id = next(iter(observer.online_monitors("cattle")))
    assert stack.docker("kill", stack.container("monitor-cattle")).returncode == 0
    # The broker publishes the monitor's Last Will.
    observer.wait_until(
        lambda: (s := observer.statuses.get(client_id)) is not None and s.state == "offline",
        "monitor-cattle offline (Last Will)",
        10,
    )
    stack.docker("start", stack.container("monitor-cattle"))
    observer.wait_until(lambda: observer.online_monitors("cattle"), "monitor-cattle online again")


def test_a_crashed_worker_leaves_and_comes_back(stack: Stack, observer: Observer) -> None:
    device = observer.active("cattle")[0]
    worker_id = device.split("-")[1]
    assert worker_id in observer.present_claims()
    assert stack.docker("kill", stack.container("device-cattle")).returncode == 0
    observer.wait_until(lambda: worker_id not in observer.present_claims(),
                        "claim removed by the Last Will", 10)  # fmt: skip
    restarted = time.time()
    stack.docker("start", stack.container("device-cattle"))
    # Same container, same hostname: it claims the same id again.
    observer.wait_until(lambda: worker_id in observer.present_claims(), "same worker id again")
    observer.wait_until(lambda: observer.samples(device, since=restarted), "telemetry again")
    assert observer.samples(device, since=restarted)[0].sequence == 1  # a new process


def test_a_second_worker_with_a_taken_id_picks_another(stack: Stack, observer: Observer) -> None:
    taken = observer.active("aquaculture")[0].split("-")[1]
    name = "monitoring-mqtt-it-collision"
    before = set(observer.present_claims())
    result = stack.compose(
        "run", "--detach", "--no-deps", "--name", name, "-e", f"WORKER_ID={taken}",
        "device-aquaculture",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    try:
        new = observer.wait_until(lambda: set(observer.present_claims()) - before,
                                  "the second worker's claim")  # fmt: skip
        (new_id,) = new
        assert new_id != taken
        claims = observer.present_claims()
        assert claims[taken].hostname != claims[new_id].hostname  # the first one kept its id
        observer.wait_until(lambda: observer.devices(f"aquaculture-{new_id}"),
                            "devices of the second worker publish")  # fmt: skip
    finally:
        stack.docker("rm", "--force", name)
        extra = [d for d in observer.devices("aquaculture") if d.split("-")[1] not in before]
        observer.wait_until(lambda: not set(extra) & set(observer.active("aquaculture", 1.5)),
                            "the second worker's devices are gone")  # fmt: skip
