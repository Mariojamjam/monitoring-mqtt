"""Entry point of the ``monitoring-mqtt`` command line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from monitoring_mqtt import __version__
from monitoring_mqtt.cli import dashboard as dashboard_command
from monitoring_mqtt.cli import device, stack, testing
from monitoring_mqtt.cli import doctor as doctor_checks
from monitoring_mqtt.cli import tui as tui_command

app = typer.Typer(
    name="monitoring-mqtt",
    help="Operate the Monitoring MQTT publish/subscribe lab.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(stack.app, name="stack")
app.add_typer(testing.app, name="test")
app.add_typer(device.app, name="device")
app.command(name="tui")(tui_command.tui)
app.command(name="dashboard")(dashboard_command.dashboard)
console = Console()

STATUS_STYLE = {
    doctor_checks.Status.OK: "[green]ok[/green]",
    doctor_checks.Status.WARN: "[yellow]warn[/yellow]",
    doctor_checks.Status.FAIL: "[red]fail[/red]",
}


@app.callback()
def main(
    ctx: typer.Context,
    project_dir: Annotated[
        Path | None,
        typer.Option(
            "--project-dir",
            help="Project checkout to operate on (overrides MONITORING_MQTT_PROJECT_DIR).",
            file_okay=False,
        ),
    ] = None,
) -> None:
    """Operate the Monitoring MQTT publish/subscribe lab."""
    ctx.obj = {"project_dir": project_dir}


@app.command()
def version() -> None:
    """Show the installed version."""
    console.print(f"monitoring-mqtt {__version__}")


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check environment, Python, uv, Docker, the project dir, roles and the broker."""
    results = doctor_checks.run_checks(ctx.obj["project_dir"])

    table = Table(title="monitoring-mqtt doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    for result in results:
        table.add_row(result.name, STATUS_STYLE[result.status], result.detail)
    console.print(table)

    if any(r.status is doctor_checks.Status.FAIL for r in results):
        console.print("[red]Some checks failed.[/red] Fix the items above and run doctor again.")
        raise typer.Exit(code=1)
    console.print("[green]Environment ready.[/green]")


if __name__ == "__main__":  # pragma: no cover
    app()
