import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from monitoring_mqtt.cli import doctor
from monitoring_mqtt.cli.doctor import Status
from monitoring_mqtt.cli.main import app

runner = CliRunner()


def completed(args: list[str], code: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(args, code, stdout=out, stderr=err)


def test_docker_checks_report_unreachable_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["docker", "info"]:
            return completed(args, 1, err="Cannot connect to the Docker daemon")
        return completed(args, 0, out="2.29.0")

    results = {r.name: r for r in doctor.check_docker(fake_run)}
    assert results["Docker daemon"].status is Status.FAIL
    assert "Cannot connect" in results["Docker daemon"].detail  # docker's own hint is kept
    assert results["Docker Compose V2"].status is Status.OK


def test_docker_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    results = doctor.check_docker(lambda args: pytest.fail("must not run docker"))
    assert [r.status for r in results] == [Status.FAIL]


@pytest.mark.parametrize(
    ("statuses", "exit_code"),
    [([Status.OK, Status.WARN], 0), ([Status.OK, Status.FAIL], 1)],
)
def test_doctor_exit_code(
    monkeypatch: pytest.MonkeyPatch, statuses: list[Status], exit_code: int
) -> None:
    def fake_checks(project_dir: Path | None) -> list[doctor.CheckResult]:
        return [doctor.CheckResult(f"check{i}", s, "detail") for i, s in enumerate(statuses)]

    monkeypatch.setattr(doctor, "run_checks", fake_checks)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == exit_code


def _unreachable(host: str, port: int) -> None:
    raise ConnectionRefusedError("refused")


def test_machine_check_local_broker_down_is_a_warning(project: Path) -> None:
    roles, broker = doctor.check_machine(project, _unreachable, environment="host (Linux)")
    assert roles.status is Status.OK
    assert broker.status is Status.WARN
    assert "127.0.0.1:1883" in broker.detail


def test_machine_check_remote_broker(project: Path) -> None:
    (project / ".env").write_text("STACK_ROLES=devices\nMQTT_BROKER_HOST=10.0.0.9\n")
    seen = []
    _, broker = doctor.check_machine(
        project, lambda host, port: seen.append((host, port)), environment="host (Linux)"
    )
    assert broker.status is Status.OK
    assert seen == [("10.0.0.9", 1883)]
    _, broker = doctor.check_machine(project, _unreachable, environment="host (Linux)")
    assert broker.status is Status.FAIL


def _docker(operating_system: str):
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        out = operating_system if args[1] == "info" else "172.17.0.1"
        return subprocess.CompletedProcess(args, 0, stdout=out + "\n", stderr="")

    return run


_bridge = _docker("Ubuntu 24.04 LTS")


def test_docker_host_alias_depends_on_docker_flavour() -> None:
    assert doctor.docker_host_address(_bridge) == "172.17.0.1"  # Docker Engine: bridge
    assert doctor.docker_host_address(_docker("Docker Desktop")) == "127.0.0.1"


def test_machine_check_tunnel_alias_and_container(project: Path) -> None:
    (project / ".env").write_text("STACK_ROLES=devices\nMQTT_BROKER_HOST=host.docker.internal\n")
    seen = []
    doctor.check_machine(
        project, lambda h, p: seen.append((h, p)), environment="host (Linux)", run=_bridge
    )
    assert seen == [("172.17.0.1", 1883)]  # what containers reach
    _, broker = doctor.check_machine(project, _unreachable, environment="devcontainer")
    assert broker.status is Status.OK and "not checked" in broker.detail


def test_machine_check_loopback_tunnel(project: Path) -> None:
    (project / ".env").write_text("STACK_ROLES=devices\nMQTT_BROKER_HOST=127.0.0.1\n")
    seen = []
    doctor.check_machine(project, lambda h, p: seen.append((h, p)), environment="host (Linux)")
    assert seen == [("127.0.0.1", 1883)]  # the tunnel itself, on the host


def test_machine_check_invalid_settings(project: Path) -> None:
    (project / ".env").write_text("STACK_ROLES=toaster\n")
    [roles] = doctor.check_machine(project, _unreachable, environment="host (Linux)")
    assert roles.status is Status.FAIL


def test_doctor_checks_the_api_on_storage_machines(project: Path) -> None:
    def answering(status: int):
        return lambda url: status

    def down(url: str) -> int:
        raise OSError("connection refused")

    (ok,) = doctor.check_api(project, answering(200), environment="host (Linux)")
    assert ok.status is doctor.Status.OK and "http://127.0.0.1:8000/docs" in ok.detail
    (no_db,) = doctor.check_api(project, answering(503), environment="host (Linux)")
    assert no_db.status is doctor.Status.FAIL and "database" in no_db.detail
    (stopped,) = doctor.check_api(project, down, environment="host (Linux)")
    assert stopped.status is doctor.Status.WARN
    (project / ".env").write_text("STACK_ROLES=broker,devices\n")
    assert doctor.check_api(project, answering(200), environment="host (Linux)") == []


def test_doctor_checks_the_dashboard(project: Path) -> None:
    (ok,) = doctor.check_dashboard(project, lambda url: 200, environment="host (Linux)")
    assert ok.status is doctor.Status.OK and "http://127.0.0.1:8501" in ok.detail
    (inside,) = doctor.check_dashboard(project, lambda url: 200, environment="devcontainer")
    assert inside.status is doctor.Status.OK and "not checked" in inside.detail
    (project / ".env").write_text("STACK_ROLES=broker,devices\n")
    assert doctor.check_dashboard(project, lambda url: 200, environment="host (Linux)") == []


def test_dashboard_command_opens_the_browser(project: Path, monkeypatch) -> None:
    from monitoring_mqtt.cli import dashboard

    opened: list[str] = []
    monkeypatch.setattr(dashboard, "detect_environment", lambda: "host (Linux)")
    monkeypatch.setattr(dashboard.webbrowser, "open", opened.append)
    monkeypatch.setattr(dashboard, "fetch_status", lambda url: 200)
    result = runner.invoke(app, ["--project-dir", str(project), "dashboard"])
    assert result.exit_code == 0 and opened == ["http://127.0.0.1:8501"]
    monkeypatch.setattr(dashboard, "fetch_status", lambda url: 502)
    result = runner.invoke(app, ["--project-dir", str(project), "dashboard", "--no-open"])
    assert result.exit_code == 1 and "stack up" in result.output and len(opened) == 1
