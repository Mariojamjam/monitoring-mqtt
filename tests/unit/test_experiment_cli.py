from pathlib import Path

from typer.testing import CliRunner

from monitoring_mqtt.cli import experiment
from monitoring_mqtt.cli.main import app
from monitoring_mqtt.experiments.probe import Observations

REPO = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_docker_stats_parsing() -> None:
    assert experiment.parse_mib("512KiB / 7.6GiB") == 0.5
    assert round(experiment.parse_mib("1.5GiB / 7.6GiB")) == 1536
    assert experiment.parse_mib("n/a") == 0.0
    samples = experiment.parse_stats(
        "aaa\t12.5%\t30MiB / 1GiB\nbbb\t0.00%\t1MiB / 1GiB\nzzz\t1%\t1MiB / 1GiB\n",
        {"aaa": "broker", "bbb": "api"},
        1.0,
    )
    assert [(s["service"], s["cpu_percent"], s["mem_mib"]) for s in samples] == [
        ("broker", 12.5, 30.0),
        ("api", 0.0, 1.0),
    ]


def test_list_and_dry_run_touch_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STACK_ROLES", "")
    result = runner.invoke(app, ["--project-dir", str(REPO), "experiment", "list"])
    assert result.exit_code == 0 and "broker-failure" in result.output
    before = set((REPO / "results").glob("quick/*")) if (REPO / "results").exists() else set()
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(REPO),
            "experiment",
            "run",
            "quick",
            "--dry-run",
            "-r",
            "2",
            "--duration",
            "15s",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "4 run(s)" in result.output
    after = set((REPO / "results").glob("quick/*")) if (REPO / "results").exists() else set()
    assert after == before


def test_run_needs_the_devices_role_and_a_valid_window(monkeypatch) -> None:
    monkeypatch.setenv("STACK_ROLES", "storage")
    monkeypatch.setenv("MQTT_BROKER_HOST", "pi.local")
    result = runner.invoke(
        app, ["--project-dir", str(REPO), "experiment", "run", "quick", "--dry-run"]
    )
    assert result.exit_code == 2 and "devices" in result.output
    monkeypatch.setenv("STACK_ROLES", "")
    monkeypatch.delenv("MQTT_BROKER_HOST")
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(REPO),
            "experiment",
            "run",
            "broker-failure",
            "--dry-run",
            "--duration",
            "30s",
        ],
    )
    assert result.exit_code == 2 and "after the window ends" in result.output


def test_probe_observations(measurement) -> None:
    from datetime import UTC, datetime

    from monitoring_mqtt.core.status import MonitorState, PublisherCounters, WorkerStatus

    obs = Observations("r1", observe_telemetry=True)
    now = datetime.now(UTC)
    status = WorkerStatus(
        worker_id="w1",
        run_id="r1",
        hostname="h",
        state=MonitorState.ONLINE,
        qos=1,
        started_at=now,
        updated_at=now,
        devices=[
            PublisherCounters(
                device_id="poultry-w1-001", generated=3, published=3, acked=3, failed=0
            )
        ],
    )
    other = status.model_copy(update={"worker_id": "w2", "run_id": "old"})
    obs.handle(status.topic, status.to_payload())
    obs.handle(other.topic, other.to_payload())  # another run: ignored
    obs.handle("$SYS/broker/messages/received", b"1234")
    obs.handle("$SYS/broker/uptime", b"10 seconds")  # not sampled
    m = measurement(device_id="poultry-w1-001")
    obs.handle(m.topic, m.to_payload())  # not recording yet
    obs.recording = True
    obs.handle(m.topic, m.to_payload())
    assert obs.online_devices() == 1
    snap = obs.snapshot()
    assert list(snap["workers"]) == ["w1"] and snap["broker"] == {"messages_received": 1234.0}
    assert list(obs.take_arrivals()) == ["poultry-w1-001"]
    assert len(obs.take_arrivals()["poultry-w1-001"]) == 1


def test_counters_are_interpolated_at_the_window_edges() -> None:
    from datetime import UTC, datetime, timedelta

    from monitoring_mqtt.core.status import MonitorState, PublisherCounters, WorkerStatus

    t0 = datetime(2026, 11, 16, 12, tzinfo=UTC)
    obs = Observations("r1", observe_telemetry=False)

    def report(seconds: int, published: int) -> None:
        status = WorkerStatus(
            worker_id="w1",
            run_id="r1",
            hostname="h",
            state=MonitorState.ONLINE,
            qos=1,
            started_at=t0,
            updated_at=t0 + timedelta(seconds=seconds),
            devices=[
                PublisherCounters(
                    device_id="poultry-w1-001",
                    generated=published,
                    published=published,
                    acked=published,
                    failed=0,
                )
            ],
        )
        obs.handle(status.topic, status.to_payload())

    for seconds in (0, 5, 10):
        report(seconds, published=10 * seconds)  # 10 messages per second
    at = t0 + timedelta(seconds=7)
    assert obs.statuses_after(at) and not obs.statuses_after(t0 + timedelta(seconds=10))
    assert obs.snapshot_at(at)["workers"]["w1"]["devices"]["poultry-w1-001"]["published"] == 70
    later = obs.snapshot_at(t0 + timedelta(seconds=30))  # no status after: the latest one
    assert later["workers"]["w1"]["devices"]["poultry-w1-001"]["published"] == 100
    earlier = obs.snapshot_at(t0 - timedelta(seconds=1))  # before the worker started
    assert earlier["workers"]["w1"]["devices"]["poultry-w1-001"]["published"] == 0
