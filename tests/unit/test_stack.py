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
        (["restart"], ["restart"]),
        (
            ["restart", "broker", "monitor-poultry", "-t", "2"],
            ["restart", "--timeout", "2", "broker", "monitor-poultry"],
        ),
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


def test_restart_preserves_compose_exit_code(project: Path, monkeypatch) -> None:
    def unknown_service(command: list[str], check: bool, **kwargs):
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(compose.subprocess, "run", unknown_service)
    result = invoke(project, "restart", "no-such-service")
    assert result.exit_code == 1


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


DEVICES_ONLY_PS = (
    "device-aquaculture aaaaaa000001\ndevice-aquaculture bbbbbb000002\n"
    "device-poultry cccccc000003\n"
)


def test_up_on_devices_machine_uses_remote_broker(stack_project: Path, monkeypatch) -> None:
    (stack_project / ".env").write_text("STACK_ROLES=devices\nMQTT_BROKER_HOST=192.168.0.50\n")
    docker = use_docker(monkeypatch, FakeDocker(DEVICES_ONLY_PS))
    result = invoke(stack_project, "up")
    assert result.exit_code == 0, result.output

    command, env = docker.calls[0]
    assert "--no-deps" in command  # no local broker to wait for
    assert env["MQTT_BROKER_HOST"] == "192.168.0.50"
    assert env["BROKER_REPLICAS"] == "0"
    assert env["DEVICE_AQUACULTURE_REPLICAS"] == "2"
    assert env["MONITOR_POULTRY_REPLICAS"] == "0"  # monitors run on another machine
    assert "remote 192.168.0.50:1883" in result.output
    # The generated run id would differ on each machine.
    assert "warning" in result.output and "--run-id" in result.output


def test_single_machine_up_keeps_local_broker(stack_project: Path, monkeypatch) -> None:
    docker = use_docker(monkeypatch, FakeDocker())
    result = invoke(stack_project, "up")
    assert result.exit_code == 0, result.output
    command, env = docker.calls[0]
    assert "--no-deps" not in command
    assert env["MQTT_BROKER_HOST"] == "broker"
    assert env["BROKER_REPLICAS"] == "1"
    assert "warning" not in result.output


def test_up_run_id_option_overrides_config(stack_project: Path, monkeypatch) -> None:
    (stack_project / ".env").write_text("STACK_ROLES=devices\nMQTT_BROKER_HOST=pi.local\n")
    docker = use_docker(monkeypatch, FakeDocker(DEVICES_ONLY_PS))
    result = invoke(stack_project, "up", "--run-id", "lab-01")
    assert result.exit_code == 0, result.output
    assert docker.calls[0][1]["RUN_ID"] == "lab-01"
    assert "warning" not in result.output


def test_up_rejects_invalid_run_id(stack_project: Path, monkeypatch) -> None:
    docker = use_docker(monkeypatch, FakeDocker())
    assert invoke(stack_project, "up", "--run-id", "bad id").exit_code == 2
    assert docker.calls == []


def test_up_applies_alternative_experiment_file(stack_project: Path, monkeypatch) -> None:
    experiment = stack_project / "pc-a.yaml"
    experiment.write_text(
        "schema_version: 1\nworkers:\n  ph_bolus: {replicas: 1, devices_per_worker: 4}\n"
    )
    docker = use_docker(monkeypatch, FakeDocker("device-cattle ffffff000001\n"))
    result = invoke(stack_project, "up", "--config", str(experiment))
    assert result.exit_code == 0, result.output
    env = docker.calls[0][1]
    assert env["DEVICE_CATTLE_DEVICES_PER_WORKER"] == "4"
    assert env["DEVICE_AQUACULTURE_REPLICAS"] == "0"
    assert "pc-a.yaml" in result.output


def test_up_rejects_inconsistent_machine_settings(stack_project: Path, monkeypatch) -> None:
    (stack_project / ".env").write_text("STACK_ROLES=devices\n")  # no broker anywhere
    docker = use_docker(monkeypatch, FakeDocker())
    result = invoke(stack_project, "up")
    assert result.exit_code == 2
    assert "MQTT_BROKER_HOST" in result.output
    assert docker.calls == []


def _claim_json(worker_id: str, hostname: str) -> str:
    return (
        f'{{"schema_version":1,"worker_id":"{worker_id}",'
        '"instance_id":"6f1c2a8e-8a3d-4c55-9c1e-2b7b8f1d2a10",'
        f'"hostname":"{hostname}","run_id":"lab-01","device_types":["oxygen_probe"],'
        '"devices":3,"started_at":"2026-11-06T12:00:00Z"}'
    )


class RegistryDocker(FakeDocker):
    def __init__(self, claims: str, broker_up: bool = True) -> None:
        super().__init__(ps_output="device-aquaculture aaaaaa000001\n")
        self.claims = claims
        self.broker_up = broker_up

    def __call__(self, command: list[str], check: bool, **kwargs):
        if "run" in command:
            self.calls.append((command, kwargs.get("env")))
            return subprocess.CompletedProcess(command, 0, stdout=self.claims, stderr="")
        if "--quiet" in command:  # broker status
            self.calls.append((command, None))
            out = "b1\n" if self.broker_up else ""
            return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")
        return super().__call__(command, check, **kwargs)


def test_workers_lists_claims_from_every_machine(stack_project: Path, monkeypatch) -> None:
    claims = "\n".join(
        [
            _claim_json("aaaaaa", "aaaaaa000001"),  # a container on this machine
            _claim_json("3f9a1c", "bbbbbb000002"),  # elsewhere, id reassigned after collision
        ]
    )
    docker = use_docker(monkeypatch, RegistryDocker(claims))
    result = invoke(stack_project, "workers")
    assert result.exit_code == 0, result.output
    run_command = next(c for c, _ in docker.calls if "run" in c)
    assert run_command[-1] == "workers" and "--no-deps" in run_command
    assert "here" in result.output and "own id" in result.output
    assert "2 worker(s), 6 device(s)" in result.output


def test_workers_requires_the_local_broker(stack_project: Path, monkeypatch) -> None:
    docker = use_docker(monkeypatch, RegistryDocker("", broker_up=False))
    assert invoke(stack_project, "workers").exit_code == 1
    assert not any("run" in c for c, _ in docker.calls)


def test_workers_with_remote_broker(stack_project: Path, monkeypatch) -> None:
    (stack_project / ".env").write_text("STACK_ROLES=devices\nMQTT_BROKER_HOST=pi.local\n")
    docker = use_docker(monkeypatch, RegistryDocker(""))
    result = invoke(stack_project, "workers")
    assert result.exit_code == 0
    assert "No workers registered" in result.output
    _, env = next((c, e) for c, e in docker.calls if "run" in c)
    assert env["MQTT_BROKER_HOST"] == "pi.local"


class TunnelDocker(FakeDocker):
    """Docker Engine (Linux) host: answers info/network inspect; relay running after up."""

    def __init__(self, operating_system: str = "Arch Linux") -> None:
        super().__init__(DEVICES_ONLY_PS + "broker-relay 999999000009\n")
        self.operating_system = operating_system

    def __call__(self, command: list[str], check: bool, **kwargs):
        if command[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(command, 0, stdout=self.operating_system, stderr="")
        if command[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(command, 0, stdout="172.17.0.1\n", stderr="")
        return super().__call__(command, check, **kwargs)


def test_up_with_tunnel_on_loopback_starts_relay(stack_project: Path, monkeypatch) -> None:
    (stack_project / ".env").write_text(
        "STACK_ROLES=devices\nMQTT_BROKER_HOST=127.0.0.1\nMQTT_BROKER_PORT=11883\n"
    )
    docker = use_docker(monkeypatch, TunnelDocker())
    result = invoke(stack_project, "up", "--run-id", "lab-01")
    assert result.exit_code == 0, result.output
    _, env = next((c, e) for c, e in docker.calls if "up" in c)
    assert env["MQTT_BROKER_HOST"] == "host.docker.internal"
    assert env["BROKER_RELAY_REPLICAS"] == "1"
    assert env["RELAY_LISTEN_HOST"] == "172.17.0.1"
    assert env["RELAY_TARGET_HOST"] == "127.0.0.1"
    assert env["RELAY_PORT"] == "11883"
    assert "172.17.0.1:11883" in result.output  # relay address shown (table may wrap)


def test_up_with_tunnel_on_docker_desktop_needs_no_relay(stack_project: Path, monkeypatch) -> None:
    (stack_project / ".env").write_text("STACK_ROLES=devices\nMQTT_BROKER_HOST=localhost\n")
    docker = use_docker(monkeypatch, TunnelDocker("Docker Desktop"))
    result = invoke(stack_project, "up", "--run-id", "lab-01")
    assert result.exit_code == 0, result.output
    _, env = next((c, e) for c, e in docker.calls if "up" in c)
    assert env["MQTT_BROKER_HOST"] == "host.docker.internal"
    assert env["BROKER_RELAY_REPLICAS"] == "0"


def test_tui_runs_the_ui_service_with_broker_and_experiment(
    stack_project: Path, monkeypatch
) -> None:
    from monitoring_mqtt.cli import tui

    monkeypatch.setattr(tui, "interactive", lambda: True)
    docker = use_docker(monkeypatch, RegistryDocker(""))  # answers the broker status check
    result = runner.invoke(app, ["--project-dir", str(stack_project), "tui"])
    assert result.exit_code == 0, result.output
    command, env = next((c, e) for c, e in docker.calls if "run" in c)
    assert command[-4:] == ["run", "--rm", "--no-deps", "tui"]
    assert env["MQTT_BROKER_HOST"] == "broker"
    assert env["PUBLISH_INTERVAL_SECONDS"] == "5"
    assert env["TUI_BROKER_LABEL"] == "broker:1883 (local)"
    assert env["MQTT_QOS"] == "1"


def test_tui_requires_a_terminal(stack_project: Path, monkeypatch) -> None:
    docker = use_docker(monkeypatch, RegistryDocker(""))
    result = runner.invoke(app, ["--project-dir", str(stack_project), "tui"])
    assert result.exit_code == 2
    assert not any("run" in c for c, _ in docker.calls)
