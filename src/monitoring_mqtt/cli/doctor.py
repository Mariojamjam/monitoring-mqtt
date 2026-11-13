"""Environment diagnostics for the ``doctor`` command."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from monitoring_mqtt.cli.dockerhost import DockerHostError, inspect_docker_host
from monitoring_mqtt.cli.machine import (
    DOCKER_HOST_ALIAS,
    STORAGE,
    Machine,
    MachineError,
    load_machine,
)
from monitoring_mqtt.cli.project import ProjectNotFoundError, resolve_project_root

REQUIRED_PYTHON = (3, 12)
COMMAND_TIMEOUT_SECONDS = 15
BROKER_CONNECT_TIMEOUT_SECONDS = 3.0


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command capturing output; never uses a shell."""
    return subprocess.run(
        args, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False
    )


DEVCONTAINER_ENV_VARS = ("DEVCONTAINER", "REMOTE_CONTAINERS", "CODESPACES")


def detect_environment(
    env: dict[str, str] | None = None, dockerenv: Path = Path("/.dockerenv")
) -> str:
    """Describe where the CLI runs: a Dev Container, another container or the host OS."""
    env = dict(os.environ) if env is None else env
    if any(env.get(name, "").lower() == "true" for name in DEVCONTAINER_ENV_VARS):
        return "devcontainer"
    if dockerenv.exists():
        return "container"
    return f"host ({platform.system() or 'unknown'})"


def check_environment() -> CheckResult:
    return CheckResult("Environment", Status.OK, detect_environment())


def check_python() -> CheckResult:
    current = sys.version_info[:2]
    version = ".".join(map(str, sys.version_info[:3]))
    if current == REQUIRED_PYTHON:
        return CheckResult("Python", Status.OK, f"{version} ({sys.executable})")
    return CheckResult("Python", Status.FAIL, f"{version}; expected 3.12.x")


def check_uv(run: Runner) -> CheckResult:
    if shutil.which("uv") is None:
        return CheckResult(
            "uv", Status.FAIL, "not found on PATH; run 'python scripts/bootstrap.py'"
        )
    result = run(["uv", "--version"])
    return CheckResult("uv", Status.OK, result.stdout.strip() or "available")


def check_cli_on_path() -> CheckResult:
    location = shutil.which("monitoring-mqtt")
    if location is None:
        return CheckResult(
            "CLI on PATH",
            Status.WARN,
            "monitoring-mqtt not on PATH; run 'python scripts/bootstrap.py' "
            "or use 'uv run monitoring-mqtt'",
        )
    return CheckResult("CLI on PATH", Status.OK, location)


def check_docker(run: Runner) -> list[CheckResult]:
    if shutil.which("docker") is None:
        return [
            CheckResult(
                "Docker CLI", Status.FAIL, "docker not found; install Docker Engine/Desktop"
            ),
        ]
    results = [CheckResult("Docker CLI", Status.OK, shutil.which("docker") or "docker")]

    # The daemon ID lets you confirm that the host and a Dev Container share one daemon.
    daemon = run(["docker", "info", "--format", "{{.ServerVersion}} on {{.Name}} (id {{.ID}})"])
    if daemon.returncode == 0:
        results.append(CheckResult("Docker daemon", Status.OK, daemon.stdout.strip()))
    else:
        hint = (daemon.stderr.strip().splitlines() or ["unreachable"])[-1]
        results.append(
            CheckResult("Docker daemon", Status.FAIL, f"{hint} (is Docker running? permissions?)")
        )

    compose = run(["docker", "compose", "version", "--short"])
    if compose.returncode == 0:
        results.append(CheckResult("Docker Compose V2", Status.OK, compose.stdout.strip()))
    else:
        results.append(
            CheckResult("Docker Compose V2", Status.FAIL, "'docker compose' plugin not available")
        )
    return results


def check_project(explicit: Path | None) -> CheckResult:
    try:
        root = resolve_project_root(explicit)
    except ProjectNotFoundError as exc:
        return CheckResult("Project directory", Status.FAIL, str(exc))
    return CheckResult("Project directory", Status.OK, f"{root.path} (via {root.source})")


Connector = Callable[[str, int], None]


def tcp_connect(host: str, port: int) -> None:
    """Open and close a TCP connection; raises ``OSError`` when unreachable."""
    with socket.create_connection((host, port), timeout=BROKER_CONNECT_TIMEOUT_SECONDS):
        pass


def docker_host_address(run: Runner) -> str | None:
    """What ``host.docker.internal`` reaches for containers, as seen from the host."""
    try:
        return inspect_docker_host(run).alias_target
    except DockerHostError:
        return None


def broker_address(
    machine: Machine, environment: str, docker_host: Callable[[], str | None]
) -> tuple[str, int] | None:
    """Where this CLI can reach the broker the services use; ``None`` if it cannot check.

    Ports on the host's loopback (the local broker, a tunnel endpoint) and the Docker host
    alias are not reachable the same way from inside a Dev Container, so they are not
    checked there.
    """
    on_host = environment.startswith("host")
    if not machine.remote_broker:
        return ("127.0.0.1", machine.host_port) if on_host else None
    host = machine.broker_host or ""
    if machine.tunnel_on_host:
        return (host, machine.broker_port) if on_host else None
    if host == DOCKER_HOST_ALIAS:
        resolved = docker_host() if on_host else None
        return (resolved, machine.broker_port) if resolved else None
    return host, machine.broker_port


def check_machine(
    explicit: Path | None,
    connect: Connector = tcp_connect,
    environment: str | None = None,
    run: Runner = run_command,
) -> list[CheckResult]:
    """Machine roles (.env) and whether the broker they point to accepts connections."""
    try:
        root = resolve_project_root(explicit)
    except ProjectNotFoundError:
        return []  # already reported by the project check
    try:
        machine = load_machine(root.path)
    except MachineError as exc:
        return [CheckResult("Stack roles", Status.FAIL, f"{exc} (.env)")]
    results = [
        CheckResult(
            "Stack roles", Status.OK, f"{machine.role_list}; broker {machine.describe_broker()}"
        )
    ]

    environment = environment or detect_environment()
    address = broker_address(machine, environment, lambda: docker_host_address(run))
    if address is None:
        results.append(
            CheckResult(
                "Broker",
                Status.WARN if environment.startswith("host") else Status.OK,
                "not checked from here; use 'monitoring-mqtt test smoke'",
            )
        )
        return results
    host, port = address
    try:
        connect(host, port)
    except OSError as exc:
        if machine.remote_broker:
            status, hint = Status.FAIL, "check the address, network, tunnel or firewall"
            if machine.broker_host == DOCKER_HOST_ALIAS:
                hint += f"; it must listen on {host}, which containers reach as {DOCKER_HOST_ALIAS}"
        else:
            status, hint = Status.WARN, "start it with 'monitoring-mqtt stack up'"
        results.append(CheckResult("Broker", status, f"{host}:{port} unreachable ({exc}); {hint}"))
    else:
        results.append(CheckResult("Broker", Status.OK, f"{host}:{port} accepts connections"))
    return results


def fetch_status(url: str, timeout: float = 3.0) -> int:
    """HTTP status of GET ``url`` (raises OSError when nothing answers)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def check_api(
    explicit: Path | None,
    fetch: Callable[[str], int] = fetch_status,
    environment: str | None = None,
) -> list[CheckResult]:
    """The API and its database, when this machine runs the storage role."""
    try:
        machine = load_machine(resolve_project_root(explicit).path)
    except (ProjectNotFoundError, MachineError):
        return []  # already reported
    if not machine.runs(STORAGE):
        return []
    environment = environment or detect_environment()
    url = f"{machine.api_url}/ready"
    if not environment.startswith("host"):
        return [CheckResult("API", Status.OK, f"not checked from {environment}; open {url}")]
    try:
        status = fetch(url)
    except OSError as exc:
        hint = "start it with 'monitoring-mqtt stack up'"
        return [CheckResult("API", Status.WARN, f"{url} unreachable ({exc}); {hint}")]
    if status == 200:
        return [CheckResult("API", Status.OK, f"{url} ready; reference {machine.api_url}/docs")]
    if status == 503:
        return [CheckResult("API", Status.FAIL, f"{url}: the API cannot reach its database")]
    return [CheckResult("API", Status.FAIL, f"{url} answered HTTP {status}")]


def run_checks(
    project_dir: Path | None, run: Runner = run_command, connect: Connector = tcp_connect
) -> list[CheckResult]:
    return [
        check_environment(),
        check_python(),
        check_uv(run),
        check_cli_on_path(),
        *check_docker(run),
        check_project(project_dir),
        *check_machine(project_dir, connect, run=run),
        *check_api(project_dir),
    ]
