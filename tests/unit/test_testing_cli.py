import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from monitoring_mqtt.cli import compose, testing
from monitoring_mqtt.cli.main import app

runner = CliRunner()

# Minimal experiment over the conftest test catalog; 5 s interval -> 15 s telemetry window.
SIMULATION = """
schema_version: 1
mqtt: {qos: 0}
workers:
  ammonia_sensor: {replicas: 1, devices_per_worker: 1}
"""


@pytest.fixture
def smoke_project(project: Path) -> Path:
    (project / "config" / "simulation.yaml").write_text(SIMULATION)
    return project


def invoke(project: Path, *args: str):
    return runner.invoke(app, ["--project-dir", str(project), "test", *args])


def test_unit_passes_args_to_pytest_and_preserves_exit_code(project: Path, monkeypatch) -> None:
    seen = {}

    def fake_run(command, cwd, check):
        seen.update(command=command, cwd=cwd)
        return subprocess.CompletedProcess(command, 3)

    monkeypatch.setattr(testing.subprocess, "run", fake_run)
    result = invoke(project, "unit", "-k", "topics", "-q")
    assert result.exit_code == 3
    assert seen["command"][-3:] == ["-k", "topics", "-q"]
    assert seen["cwd"] == project.resolve()


def test_unit_without_uv_exits_127(project: Path, monkeypatch) -> None:
    def missing(command, cwd, check):
        raise FileNotFoundError("uv")

    monkeypatch.setattr(testing.subprocess, "run", missing)
    assert invoke(project, "unit").exit_code == 127


class FakeCompose:
    def __init__(self, broker_running: bool, smoke_code: int = 0) -> None:
        self.broker_running = broker_running
        self.smoke_code = smoke_code
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []

    def __call__(self, command: list[str], check: bool, **kwargs):
        self.calls.append(command)
        self.envs.append(kwargs.get("env"))
        if "ps" in command:
            out = "abc123\n" if self.broker_running else ""
            return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(command, self.smoke_code)


@pytest.mark.parametrize("smoke_code", [0, 1])
def test_smoke_preserves_exit_code(smoke_project: Path, monkeypatch, smoke_code: int) -> None:
    fake = FakeCompose(broker_running=True, smoke_code=smoke_code)
    monkeypatch.setattr(compose.subprocess, "run", fake)
    assert invoke(smoke_project, "smoke").exit_code == smoke_code
    assert "smoke" in fake.calls[-1]


def test_smoke_uses_experiment_settings_and_derived_window(
    smoke_project: Path, monkeypatch
) -> None:
    fake = FakeCompose(broker_running=True)
    monkeypatch.setattr(compose.subprocess, "run", fake)
    assert invoke(smoke_project, "smoke").exit_code == 0
    env = fake.envs[-1]
    assert env["MQTT_QOS"] == "0"  # same MQTT settings as the experiment
    assert env["SMOKE_TELEMETRY_WINDOW_SECONDS"] == "15"  # 2 x 5 s interval + 5 s


def test_smoke_rejects_invalid_experiment_before_calling_docker(
    smoke_project: Path, monkeypatch
) -> None:
    fake = FakeCompose(broker_running=True)
    monkeypatch.setattr(compose.subprocess, "run", fake)
    (smoke_project / "config" / "simulation.yaml").write_text("schema_version: 2\n")
    assert invoke(smoke_project, "smoke").exit_code == 2
    assert fake.calls == []


def test_smoke_refuses_when_stack_is_down(smoke_project: Path, monkeypatch) -> None:
    fake = FakeCompose(broker_running=False)
    monkeypatch.setattr(compose.subprocess, "run", fake)
    assert invoke(smoke_project, "smoke").exit_code == 1
    assert len(fake.calls) == 1  # only the status check; no container started


def test_smoke_against_remote_broker(smoke_project: Path, monkeypatch) -> None:
    (smoke_project / ".env").write_text("STACK_ROLES=monitors\nMQTT_BROKER_HOST=pi.local\n")
    fake = FakeCompose(broker_running=False)
    monkeypatch.setattr(compose.subprocess, "run", fake)
    assert invoke(smoke_project, "smoke").exit_code == 0
    assert len(fake.calls) == 1  # no local broker check: the broker runs elsewhere
    env = fake.envs[-1]
    assert env["MQTT_BROKER_HOST"] == "pi.local"
    assert env["SMOKE_REQUIRE_TELEMETRY"] == "false"  # devices run on another machine


def test_smoke_requires_telemetry_on_single_machine(smoke_project: Path, monkeypatch) -> None:
    fake = FakeCompose(broker_running=True)
    monkeypatch.setattr(compose.subprocess, "run", fake)
    assert invoke(smoke_project, "smoke").exit_code == 0
    assert fake.envs[-1]["SMOKE_REQUIRE_TELEMETRY"] == "true"
    assert fake.envs[-1]["MQTT_BROKER_HOST"] == "broker"
