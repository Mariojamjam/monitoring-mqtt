import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from monitoring_mqtt.cli import compose
from monitoring_mqtt.cli.main import app
from monitoring_mqtt.cli.simulation import MonitorPlan, ServicePlan, StackPlan
from monitoring_mqtt.cli.stack import verify_stack

runner = CliRunner()

TOPOLOGY = """
schema_version: 1
workers:
  oxygen_probe: {replicas: 2, devices_per_worker: 3}
  ammonia_sensor: {replicas: 1, devices_per_worker: 1}
monitors:
  poultry: {replicas: 2}
"""

HEALTHY_PS = (
    "device-aquaculture aaaaaa000001\ndevice-aquaculture bbbbbb000002\n"
    "device-poultry cccccc000003\n"
    "monitor-poultry dddddd000004\nmonitor-poultry eeeeee000005\n"
)


@pytest.fixture
def stack_project(project: Path) -> Path:
    (project / "config" / "simulation.yaml").write_text(TOPOLOGY)
    return project


class FakeDocker:
    """Records compose invocations; answers `ps` with the configured containers."""

    def __init__(self, ps_output: str = HEALTHY_PS, up_code: int = 0) -> None:
        self.calls: list[tuple[list[str], dict | None]] = []
        self.ps_output = ps_output
        self.up_code = up_code

    def __call__(self, command: list[str], check: bool, **kwargs):
        self.calls.append((command, kwargs.get("env")))
        is_ps = "ps" in command and "--format" in command
        code = self.up_code if "up" in command else 0
        return subprocess.CompletedProcess(
            command, code, stdout=self.ps_output if is_ps else "", stderr=""
        )


def use_docker(monkeypatch: pytest.MonkeyPatch, fake: FakeDocker) -> FakeDocker:
    monkeypatch.setattr(compose.subprocess, "run", fake)
    return fake


def invoke(project: Path, *args: str):
    return runner.invoke(app, ["--project-dir", str(project), "stack", *args])


def test_compose_always_targets_fixed_project_and_file(project: Path) -> None:
    # Host and Dev Container must manage the same stack from any working directory.
    argv = compose.build_command(project)
    assert argv[:2] == ["docker", "compose"]
    assert argv[argv.index("--project-name") + 1] == "monitoring-mqtt"
    assert argv[argv.index("--file") + 1] == str(project / "compose.yaml")


@pytest.mark.parametrize(
    ("cli_args", "compose_args"),
    [
        (["down"], ["down", "--remove-orphans"]),
        (["logs"], ["logs", "--tail", "100", "--follow"]),
        (["logs", "broker", "--no-follow", "-n", "5"], ["logs", "--tail", "5", "broker"]),
    ],
)
def test_commands_map_to_compose(
    project: Path, monkeypatch, cli_args: list[str], compose_args: list[str]
) -> None:
    docker = use_docker(monkeypatch, FakeDocker())
    result = invoke(project, *cli_args)
    assert result.exit_code == 0, result.output
    command, _ = docker.calls[0]
    assert command[len(compose.build_command(project.resolve())) :] == compose_args


def test_up_applies_topology_through_compose_variables(stack_project: Path, monkeypatch) -> None:
    docker = use_docker(monkeypatch, FakeDocker())
    result = invoke(stack_project, "up")
    assert result.exit_code == 0, result.output

    command, env = docker.calls[0]
    assert "--wait" in command  # replicas are verified right after start-up
    assert env is not None
    assert env["DEVICE_AQUACULTURE_REPLICAS"] == "2"
    assert env["DEVICE_AQUACULTURE_DEVICES_PER_WORKER"] == "3"
    assert env["DEVICE_POULTRY_REPLICAS"] == "1"
    assert env["DEVICE_CATTLE_REPLICAS"] == "0"  # omitted from the topology
    assert env["MONITOR_POULTRY_REPLICAS"] == "2"
    assert env["MONITOR_AQUACULTURE_REPLICAS"] == "0"
    # Experiment parameters (defaults: the topology sets no run/mqtt/devices sections).
    assert env["MQTT_QOS"] == "1"
    assert env["PUBLISH_INTERVAL_SECONDS"] == "5"
    # run_id is null, so one id is generated for this run and shown to the user.
    assert env["RUN_ID"].startswith("run-")
    assert env["RUN_ID"] in result.output


def test_up_reuses_configured_run_id(stack_project: Path, monkeypatch) -> None:
    docker = use_docker(monkeypatch, FakeDocker())
    config = stack_project / "config" / "simulation.yaml"
    config.write_text(config.read_text() + "run: {run_id: qos-trial-1}\n")
    assert invoke(stack_project, "up").exit_code == 0
    _, env = docker.calls[0]
    assert env["RUN_ID"] == "qos-trial-1"


def test_up_fails_when_replicas_are_missing(stack_project: Path, monkeypatch) -> None:
    use_docker(monkeypatch, FakeDocker("device-poultry cccccc000003\nmonitor-poultry dddddd04\n"))
    result = invoke(stack_project, "up")
    assert result.exit_code == 1
    assert "device-aquaculture" in result.output


def test_up_fails_on_worker_id_collision(stack_project: Path, monkeypatch) -> None:
    ps = (
        "device-aquaculture abcdef000001\ndevice-aquaculture bbbbbb000002\n"
        "device-poultry abcdef000003\n"
        "monitor-poultry dddddd000004\nmonitor-poultry eeeeee000005\n"
    )
    use_docker(monkeypatch, FakeDocker(ps))
    result = invoke(stack_project, "up")
    assert result.exit_code == 1
    assert "abcdef" in result.output


def test_up_rejects_invalid_topology_before_calling_docker(
    stack_project: Path, monkeypatch
) -> None:
    docker = use_docker(monkeypatch, FakeDocker())
    (stack_project / "config" / "simulation.yaml").write_text(
        "schema_version: 1\nworkers:\n  toaster: {replicas: 1, devices_per_worker: 1}\n"
    )
    assert invoke(stack_project, "up").exit_code == 2
    assert docker.calls == []


def test_up_preserves_compose_exit_code(stack_project: Path, monkeypatch) -> None:
    use_docker(monkeypatch, FakeDocker(up_code=17))
    assert invoke(stack_project, "up").exit_code == 17


def test_missing_docker_exits_127(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_not_found(command: list[str], check: bool, **kwargs) -> None:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(compose.subprocess, "run", raise_not_found)
    assert invoke(project, "status").exit_code == compose.DOCKER_NOT_FOUND_EXIT


@pytest.mark.parametrize("broken", ["compose file", "project dir"])
def test_unusable_project_fails_before_calling_docker(
    project: Path, tmp_path_factory, monkeypatch, broken: str
) -> None:
    docker = use_docker(monkeypatch, FakeDocker())
    if broken == "compose file":
        (project / "compose.yaml").unlink()
        target = project
    else:
        target = tmp_path_factory.mktemp("not-a-checkout")
    assert invoke(target, "status").exit_code == 2
    assert docker.calls == []


def test_verify_stack_accepts_zero_replicas() -> None:
    plan = StackPlan(
        [ServicePlan("ph_bolus", "cattle", "device-cattle", 0, 1)],
        [MonitorPlan("all", "monitor-all", "monitoring/v1/#", 0)],
    )
    assert verify_stack(plan, {"device-cattle": [], "monitor-all": []}) == []


def test_device_and_monitor_with_same_short_id_do_not_collide() -> None:
    # Their client ids differ by prefix (cattle-... vs monitor-cattle-...).
    plan = StackPlan(
        [ServicePlan("ph_bolus", "cattle", "device-cattle", 1, 1)],
        [MonitorPlan("cattle", "monitor-cattle", "monitoring/v1/cattle/#", 1)],
    )
    running = {"device-cattle": ["abcdef000001"], "monitor-cattle": ["abcdef000002"]}
    assert verify_stack(plan, running) == []
