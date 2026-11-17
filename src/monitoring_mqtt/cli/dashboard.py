"""``monitoring-mqtt dashboard``: open the dashboard in a browser.

The dashboard is a service of the stack (``stack up`` starts it); this command only finds
its address, checks that it answers and opens it. It never starts anything.
"""

from __future__ import annotations

import webbrowser
from typing import Annotated

import typer
from rich.console import Console

from monitoring_mqtt.cli.doctor import detect_environment, fetch_status
from monitoring_mqtt.cli.machine import DASHBOARD
from monitoring_mqtt.cli.stack import _project_root, load_machine_or_exit

console = Console()
err_console = Console(stderr=True)


def dashboard(
    ctx: typer.Context,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open it in the default browser.")
    ] = True,
) -> None:
    """Open the dashboard (history, charts, alerts, runs) in the browser."""
    machine = load_machine_or_exit(_project_root(ctx))
    url = machine.dashboard_url
    if not machine.runs(DASHBOARD):
        err_console.print(
            f"[yellow]warning:[/yellow] this machine does not run the dashboard role "
            f"(.env STACK_ROLES={machine.role_list}); it may run on another machine."
        )
    environment = detect_environment()
    if not environment.startswith("host"):
        # Inside a container, the published port belongs to the host: just show it.
        console.print(f"Dashboard: {url} (open it in a browser on the host)")
        return
    try:
        status = fetch_status(f"{url}/_stcore/health")
    except OSError:
        status = None
    if status != 200:
        err_console.print(
            f"[red]error:[/red] the dashboard does not answer at {url}; start the stack "
            "with 'monitoring-mqtt stack up'."
        )
        raise typer.Exit(code=1)
    console.print(f"Dashboard: {url}")
    if open_browser:
        webbrowser.open(url)
