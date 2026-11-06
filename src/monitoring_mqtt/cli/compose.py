"""Thin wrapper around ``docker compose`` used by the stack commands.

Commands are always executed with an argument list (never through a shell) and the
Compose exit code is returned unchanged to the caller.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

COMPOSE_PROJECT_NAME = "monitoring-mqtt"
COMPOSE_FILE = "compose.yaml"
DOCKER_NOT_FOUND_EXIT = 127
INTERRUPTED_EXIT = 130


class ComposeError(Exception):
    """Raised when docker compose cannot be invoked at all."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def compose_file(project_root: Path) -> Path:
    return project_root / COMPOSE_FILE


def build_command(project_root: Path, *args: str) -> list[str]:
    """Build the full ``docker compose`` argv for the project."""
    return [
        "docker",
        "compose",
        "--project-name",
        COMPOSE_PROJECT_NAME,
        "--project-directory",
        str(project_root),
        "--file",
        str(compose_file(project_root)),
        *args,
    ]


def _check_compose_file(project_root: Path) -> None:
    if not compose_file(project_root).is_file():
        raise ComposeError(f"{compose_file(project_root)} not found.", exit_code=2)


def _environment(extra: Mapping[str, str] | None) -> dict[str, str] | None:
    """Current environment plus ``extra`` variables (used for compose.yaml interpolation)."""
    return {**os.environ, **extra} if extra else None


def capture(
    project_root: Path, *args: str, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run docker compose capturing stdout/stderr (for commands whose output we parse)."""
    _check_compose_file(project_root)
    try:
        return subprocess.run(
            build_command(project_root, *args),
            capture_output=True,
            text=True,
            check=False,
            env=_environment(env),
        )
    except FileNotFoundError as exc:
        raise ComposeError(
            "docker executable not found. Install Docker Engine/Desktop with Compose V2 "
            "and run 'monitoring-mqtt doctor'.",
            exit_code=DOCKER_NOT_FOUND_EXIT,
        ) from exc


def run(project_root: Path, *args: str, env: Mapping[str, str] | None = None) -> int:
    """Run docker compose with inherited stdio and return its exit code."""
    _check_compose_file(project_root)
    command = build_command(project_root, *args)
    try:
        return subprocess.run(command, check=False, env=_environment(env)).returncode
    except FileNotFoundError as exc:
        raise ComposeError(
            "docker executable not found. Install Docker Engine/Desktop with Compose V2 "
            "and run 'monitoring-mqtt doctor'.",
            exit_code=DOCKER_NOT_FOUND_EXIT,
        ) from exc
    except KeyboardInterrupt:
        # Ctrl+C while following logs is a normal way to stop; the stack keeps running.
        return INTERRUPTED_EXIT
