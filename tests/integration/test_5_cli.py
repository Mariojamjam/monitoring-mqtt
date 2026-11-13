"""The CLI end to end against the isolated stack."""

from harness import EXPERIMENT, Observer, Stack


def test_workers_lists_every_registered_worker(stack: Stack, observer: Observer) -> None:
    result = stack.cli("stack", "workers")
    assert result.returncode == 0, result.stderr
    for worker_id in observer.present_claims():
        assert worker_id in result.stdout


def test_smoke_test_passes(stack: Stack) -> None:
    result = stack.cli("test", "smoke", "--config", str(EXPERIMENT))
    assert result.returncode == 0, result.stdout + result.stderr


def test_restart_brings_a_monitor_back(stack: Stack, observer: Observer) -> None:
    (client_id, before) = next(iter(observer.online_monitors("cattle").items()))
    result = stack.cli("stack", "restart", "monitor-cattle")
    assert result.returncode == 0, result.stderr
    # Same container and client id, new process: a later start time.
    observer.wait_until(
        lambda: (
            (s := observer.statuses.get(client_id)) is not None
            and s.state == "online"
            and s.started_at > before.started_at
        ),
        "monitor-cattle restarted",
    )
