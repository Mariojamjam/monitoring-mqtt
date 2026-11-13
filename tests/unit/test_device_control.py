import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from monitoring_mqtt.cli import compose
from monitoring_mqtt.cli.main import app
from monitoring_mqtt.simulator.identity import worker_of_device
from monitoring_mqtt.tools import control

runner = CliRunner()
DOMAINS = {"aquaculture", "poultry", "cattle"}


@pytest.mark.parametrize(
    ("device_id", "worker"),
    [
        ("poultry-3f9a1c-002", "3f9a1c"),
        ("aquaculture-my-lab-01-012", "my-lab-01"),  # worker ids may contain '-'
        ("cattle-abc-1000", "abc"),
    ],
)
def test_worker_of_device(device_id: str, worker: str) -> None:
    assert worker_of_device(device_id, DOMAINS) == worker


@pytest.mark.parametrize("device_id", ["poultry-002", "goats-abc-001", "poultry-abc", "x"])
def test_not_a_device_id(device_id: str) -> None:
    with pytest.raises(ValueError, match="not a device id"):
        worker_of_device(device_id, DOMAINS)


def test_control_tool_rejects_bad_arguments() -> None:
    assert control.main(["poultry-abc-001", "explode"]) == control.INVALID
    assert control.main(["not-a-device", "stop"]) == control.INVALID


class ControlDocker:
    """Broker running; the control service answers with ``answer``."""

    def __init__(self, code: int, ack: dict | None = None, stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict | None]] = []
        self.code, self.ack, self.stderr = code, ack, stderr

    def __call__(self, command: list[str], check: bool, **kwargs):
        self.calls.append((command, kwargs.get("env")))
        if "ps" in command:
            return subprocess.CompletedProcess(command, 0, stdout="b1\n", stderr="")
        out = json.dumps(self.ack) + "\n" if self.ack else ""
        return subprocess.CompletedProcess(command, self.code, stdout=out, stderr=self.stderr)


def ack(accepted: bool, detail: str) -> dict:
    return {"worker_id": "3f9a1c", "accepted": accepted, "detail": detail}


def invoke(project: Path, docker: ControlDocker, monkeypatch, *args: str):
    monkeypatch.setattr(compose.subprocess, "run", docker)
    return runner.invoke(app, ["--project-dir", str(project), "device", *args])


def run_args(docker: ControlDocker) -> list[str]:
    command = next(c for c, _ in docker.calls if "run" in c)
    return command[command.index("control") + 1 :]


def test_stop_for_a_while(project: Path, monkeypatch) -> None:
    docker = ControlDocker(0, ack(True, "applies at the device's next publish time"))
    result = invoke(project, docker, monkeypatch, "stop", "poultry-3f9a1c-002", "--for", "2m")
    assert result.exit_code == 0, result.output
    assert run_args(docker) == ["poultry-3f9a1c-002", "stop", "--lasts", "120"]
    assert "accepted by worker 3f9a1c" in result.output


def test_anomaly_direction_and_resume(project: Path, monkeypatch) -> None:
    docker = ControlDocker(0, ack(True, "ok"))
    invoke(project, docker, monkeypatch, "anomaly", "cattle-abc-001", "--direction", "low")
    assert run_args(docker) == ["cattle-abc-001", "anomaly", "--direction", "low"]
    docker = ControlDocker(0, ack(True, "ok"))
    invoke(project, docker, monkeypatch, "resume", "cattle-abc-001")
    assert run_args(docker) == ["cattle-abc-001", "resume"]


def test_rejected_and_unanswered_commands(project: Path, monkeypatch) -> None:
    rejected = ControlDocker(1, ack(False, "this device type has no low anomaly"))
    result = invoke(project, rejected, monkeypatch, "anomaly", "poultry-abc-001", "--direction",
                    "low")  # fmt: skip
    assert result.exit_code == 1 and "no low anomaly" in result.output
    nobody = ControlDocker(3)
    result = invoke(project, nobody, monkeypatch, "pause", "poultry-zzz-001")
    assert result.exit_code == 3 and "no worker answered" in result.output


def test_invalid_duration_never_calls_docker(project: Path, monkeypatch) -> None:
    docker = ControlDocker(0)
    result = invoke(project, docker, monkeypatch, "pause", "poultry-abc-001", "--for", "30")
    assert result.exit_code == 2 and "unit" in result.output
    assert not any("run" in c for c, _ in docker.calls)
