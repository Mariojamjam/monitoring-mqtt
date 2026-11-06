"""``monitoring-mqtt test`` commands."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer
from rich.console import Console

from monitoring_mqtt.cli import compose
from monitoring_mqtt.cli.machine import DEVICES
from monitoring_mqtt.cli.project import ProjectNotFoundError, resolve_project_root
from monitoring_mqtt.cli.simulation import smoke_env
from monitoring_mqtt.cli.stack import (
    ConfigOption,
    broker_env,
    load_machine_or_exit,
    load_simulation_or_exit,
)

SMOKE_SERVICE = "smoke"
UNIT_TESTS = "tests/unit"

app = typer.Typer(help="Run automated tests.", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)

PASSTHROUGH = {"allow_extra_args": True, "ignore_unknown_options": True}


def _project_root(ctx: typer.Context) -> Path:
    explicit = (ctx.find_root().obj or {}).get("project_dir")
    try:
        return resolve_project_root(explicit).path
    except ProjectNotFoundError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def unit_command(extra: list[str]) -> list[str]:
    # Development dependencies (pytest) live in the project environment, not in the
    # installed CLI, so the suite always runs through uv with the locked versions.
    return ["uv", "run", "--frozen", "pytest", UNIT_TESTS, *extra]


@app.command(context_settings=PASSTHROUGH)
def unit(ctx: typer.Context) -> None:
    """Run the unit tests (extra arguments go to pytest, e.g. -k topics)."""
    root = _project_root(ctx)
    try:
        code = subprocess.run(unit_command(list(ctx.args)), cwd=root, check=False).returncode
    except FileNotFoundError as exc:
        err_console.print(
            "[red]error:[/red] uv not found. Run 'python scripts/bootstrap.py' or install uv."
        )
        raise typer.Exit(code=compose.DOCKER_NOT_FOUND_EXIT) from exc
    raise typer.Exit(code=code)


@app.command()
def smoke(ctx: typer.Context, config: ConfigOption = None) -> None:
    """Check the broker this machine uses end to end, from a container.

    Live telemetry is required only when this machine runs devices.
    """
    root = _project_root(ctx)
    machine = load_machine_or_exit(root)
    # Same MQTT settings as the experiment; the telemetry window follows its interval.
    sim = load_simulation_or_exit(root, config)
    try:
        env = {
            **smoke_env(sim),
            **broker_env(root, machine),
            "SMOKE_REQUIRE_TELEMETRY": "true" if machine.runs(DEVICES) else "false",
        }
        code = compose.run(root, "run", "--rm", "--no-deps", SMOKE_SERVICE, env=env)
    except compose.ComposeError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    if code == 0:
        console.print("[green]Smoke test passed.[/green]")
    else:
        err_console.print(f"[red]Smoke test failed (exit code {code}).[/red]")
    raise typer.Exit(code=code)
