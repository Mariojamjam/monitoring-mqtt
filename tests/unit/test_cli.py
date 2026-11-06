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
