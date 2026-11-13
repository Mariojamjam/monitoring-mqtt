"""``monitoring-mqtt tui``: open the read-only terminal UI.

The UI runs as the one-off ``tui`` Compose service, so it reaches the broker exactly like
the other services (local stack, remote broker or relay), from the host or a Dev Container.
The CLI only starts it; the UI talks to the broker, never to Docker.
"""

from __future__ import annotations

import sys

import typer
from rich.console import Console

from monitoring_mqtt.cli import compose
from monitoring_mqtt.cli.simulation import mqtt_env
from monitoring_mqtt.cli.stack import (
    ConfigOption,
    _project_root,
    broker_env,
    load_machine_or_exit,
    load_simulation_or_exit,
)

TUI_SERVICE = "tui"
err_console = Console(stderr=True)


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def tui(ctx: typer.Context, config: ConfigOption = None) -> None:
    """Open the terminal UI: who publishes, the broker, who consumes (read-only)."""
    root = _project_root(ctx)
    machine = load_machine_or_exit(root)
    sim = load_simulation_or_exit(root, config)
    if not interactive():
        err_console.print("[red]error:[/red] the TUI needs an interactive terminal.")
        raise typer.Exit(code=2)
    try:
        env = {
            **mqtt_env(sim),
            **broker_env(root, machine),
            # Tells the UI when a silent device counts as stale.
            "PUBLISH_INTERVAL_SECONDS": f"{sim.devices.publish_interval_seconds:g}",
            "TUI_BROKER_LABEL": machine.broker_label,
        }
        code = compose.run(root, "run", "--rm", "--no-deps", TUI_SERVICE, env=env)
    except compose.ComposeError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc
    raise typer.Exit(code=0 if code == compose.INTERRUPTED_EXIT else code)
