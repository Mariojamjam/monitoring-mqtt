"""``monitoring-mqtt test`` commands."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer
from rich.console import Console

from monitoring_mqtt.cli import compose
from monitoring_mqtt.cli.project import ProjectNotFoundError, resolve_project_root
from monitoring_mqtt.cli.simulation import SimulationError, load_simulation, smoke_env

SMOKE_SERVICE = "smoke"
BROKER_SERVICE = "broker"
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


def broker_running(root: Path) -> bool:
    result = compose.capture(root, "ps", "--status", "running", "--quiet", BROKER_SERVICE)
    return result.returncode == 0 and bool(result.stdout.strip())


@app.command()
def smoke(ctx: typer.Context) -> None:
    """Check the running stack end to end from inside the Compose network."""
    root = _project_root(ctx)
    try:
        # Same MQTT settings as the experiment; the telemetry window follows its interval.
        env = smoke_env(load_simulation(root))
    except SimulationError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    try:
        if not broker_running(root):
            err_console.print(
                "[red]error:[/red] the stack is not running. "
                "Start it with 'monitoring-mqtt stack up' first."
            )
            raise typer.Exit(code=1)
        code = compose.run(root, "run", "--rm", "--no-deps", SMOKE_SERVICE, env=env)
    except compose.ComposeError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    if code == 0:
        console.print("[green]Smoke test passed.[/green]")
    else:
        err_console.print(f"[red]Smoke test failed (exit code {code}).[/red]")
    raise typer.Exit(code=code)
