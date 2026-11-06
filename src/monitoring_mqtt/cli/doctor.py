"""Environment diagnostics for the ``doctor`` command."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from monitoring_mqtt.cli.project import ProjectNotFoundError, resolve_project_root

REQUIRED_PYTHON = (3, 12)
COMMAND_TIMEOUT_SECONDS = 15


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


def run_checks(project_dir: Path | None, run: Runner = run_command) -> list[CheckResult]:
    return [
        check_environment(),
        check_python(),
        check_uv(run),
        check_cli_on_path(),
        *check_docker(run),
        check_project(project_dir),
    ]
