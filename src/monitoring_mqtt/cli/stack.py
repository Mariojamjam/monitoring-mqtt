"""``monitoring-mqtt stack`` commands: manage the Docker Compose stack."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from monitoring_mqtt.cli import compose
from monitoring_mqtt.cli.project import ProjectNotFoundError, resolve_project_root
from monitoring_mqtt.cli.simulation import (
    SIMULATION_FILE,
    MonitorPlan,
    ServicePlan,
    Simulation,
    SimulationError,
    StackPlan,
    compose_env,
    load_simulation,
    resolve_run_id,
)
from monitoring_mqtt.core.identity import host_short_id

app = typer.Typer(help="Manage the Docker Compose stack.", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)


def _project_root(ctx: typer.Context) -> Path:
    explicit = (ctx.obj or {}).get("project_dir")
    try:
        return resolve_project_root(explicit).path
    except ProjectNotFoundError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _run(root: Path, *args: str, env: dict[str, str] | None = None) -> int:
    """Run a compose subcommand, report failures and return the exit code."""
    try:
        code = compose.run(root, *args, env=env)
    except compose.ComposeError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc
    if code not in (0, compose.INTERRUPTED_EXIT):
        err_console.print(f"[red]docker compose exited with code {code}.[/red]")
    return code


def _compose(ctx: typer.Context, *args: str) -> None:
    """Run a compose subcommand and exit with its exit code."""
    raise typer.Exit(code=_run(_project_root(ctx), *args))


def find_duplicate_worker_ids(container_ids: list[str]) -> dict[str, list[str]]:
    """Group containers whose derived worker id (first hostname chars) collide."""
    groups: dict[str, list[str]] = {}
    for container_id in container_ids:
        groups.setdefault(host_short_id(container_id), []).append(container_id)
    return {wid: ids for wid, ids in groups.items() if len(ids) > 1}


def running_containers(root: Path, services: list[str]) -> dict[str, list[str]]:
    """Container ids of the running containers of ``services``, grouped by service."""
    try:
        result = compose.capture(root, "ps", "--format", "{{.Service}} {{.ID}}", *services)
    except compose.ComposeError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc
    if result.returncode != 0:
        err_console.print(f"[red]could not list containers:[/red] {result.stderr}")
        raise typer.Exit(code=result.returncode)
    running: dict[str, list[str]] = {service: [] for service in services}
    for line in result.stdout.splitlines():
        service, _, container_id = line.strip().partition(" ")
        if service in running and container_id:
            running[service].append(container_id)
    return running


def _check_counts(expected: dict[str, int], running: dict[str, list[str]]) -> list[str]:
    problems = []
    for service, replicas in expected.items():
        found = len(running.get(service, []))
        if found != replicas:
            problems.append(f"{service}: expected {replicas} container(s), found {found}")
    return problems


def _check_unique_ids(running: dict[str, list[str]], services: list[str]) -> list[str]:
    container_ids = [cid for service in services for cid in running.get(service, [])]
    return [
        f"containers {', '.join(ids)} share id {short_id}: their MQTT client ids would "
        "collide; run 'monitoring-mqtt stack down' and 'stack up' again"
        for short_id, ids in sorted(find_duplicate_worker_ids(container_ids).items())
    ]


def verify_stack(plan: StackPlan, running: dict[str, list[str]]) -> list[str]:
    """Problems found after start-up: missing replicas or colliding client ids."""
    expected = {w.service: w.replicas for w in plan.workers}
    expected.update({m.service: m.replicas for m in plan.monitors})
    return [
        *_check_counts(expected, running),
        # Device and monitor client ids embed the container's short id; check each group.
        *_check_unique_ids(running, [w.service for w in plan.workers]),
        *_check_unique_ids(running, [m.service for m in plan.monitors]),
    ]


def _short_ids(running: dict[str, list[str]], service: str) -> str:
    return ", ".join(sorted(host_short_id(cid) for cid in running.get(service, []))) or "-"


def print_workers(workers: list[ServicePlan], running: dict[str, list[str]]) -> None:
    table = Table(
        title=f"Publishers ({SIMULATION_FILE.as_posix()}: workers)",
        caption="N = worker containers, M = devices per container",
    )
    table.add_column("Device type", no_wrap=True)
    table.add_column("Service", no_wrap=True)
    table.add_column("N", justify="right")
    table.add_column("M", justify="right")
    table.add_column("Devices", justify="right")
    table.add_column("Worker ids")
    for w in workers:
        table.add_row(
            w.device_type,
            w.service,
            str(w.replicas),
            str(w.devices_per_worker),
            str(w.devices),
            _short_ids(running, w.service),
        )
    console.print(table)


def print_monitors(monitors: list[MonitorPlan], running: dict[str, list[str]]) -> None:
    table = Table(
        title=f"Subscribers ({SIMULATION_FILE.as_posix()}: monitors)",
        caption="Each replica is an independent subscriber and receives its own copy",
    )
    table.add_column("Monitor", no_wrap=True)
    table.add_column("Service", no_wrap=True)
    table.add_column("Filter", no_wrap=True)
    table.add_column("N", justify="right")
    table.add_column("Ids")
    for m in monitors:
        table.add_row(
            m.kind, m.service, m.topic_filter, str(m.replicas), _short_ids(running, m.service)
        )
    console.print(table)


def print_experiment(sim: Simulation, run_id: str) -> None:
    mqtt = sim.mqtt
    table = Table(title=f"Experiment ({SIMULATION_FILE.as_posix()})", show_header=False)
    table.add_column("Parameter", no_wrap=True)
    table.add_column("Value")
    generated = "" if sim.run.run_id else " (generated for this run)"
    table.add_row("run id", f"{run_id}{generated}")
    table.add_row("seed", str(sim.run.seed))
    table.add_row("log level", sim.run.log_level)
    table.add_row("publish interval", f"{sim.devices.publish_interval_seconds:g} s")
    table.add_row("MQTT QoS", str(mqtt.qos))
    table.add_row("MQTT keepalive", f"{mqtt.keepalive_seconds} s")
    table.add_row("MQTT clean session", str(mqtt.clean_session).lower())
    table.add_row(
        "MQTT reconnect",
        f"{mqtt.reconnect.min_delay_seconds}-{mqtt.reconnect.max_delay_seconds} s backoff",
    )
    console.print(table)


def print_topology(plan: StackPlan, running: dict[str, list[str]]) -> None:
    print_workers(plan.workers, running)
    print_monitors(plan.monitors, running)
    console.print(
        f"Total: {sum(w.devices for w in plan.workers)} device(s) in "
        f"{sum(w.replicas for w in plan.workers)} worker container(s); "
        f"{sum(m.replicas for m in plan.monitors)} monitor container(s). "
        "One MQTT connection per device and per monitor."
    )


@app.command()
def up(
    ctx: typer.Context,
    build: Annotated[bool, typer.Option("--build", help="Build images before starting.")] = False,
) -> None:
    """Start the experiment defined in config/simulation.yaml (a new run each time)."""
    root = _project_root(ctx)
    try:
        sim = load_simulation(root)
    except SimulationError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    args = ["up", "--detach", "--wait", "--remove-orphans"]
    if build:
        args.append("--build")
    plan = sim.plan
    run_id = resolve_run_id(sim)
    code = _run(root, *args, env=compose_env(sim, run_id))
    if code != 0:
        raise typer.Exit(code=code)

    services = [w.service for w in plan.workers] + [m.service for m in plan.monitors]
    running = running_containers(root, services)
    print_experiment(sim, run_id)
    print_topology(plan, running)
    problems = verify_stack(plan, running)
    for problem in problems:
        err_console.print(f"[red]error:[/red] {problem}")
    raise typer.Exit(code=1 if problems else 0)


@app.command()
def down(ctx: typer.Context) -> None:
    """Stop and remove the stack containers and network."""
    _compose(ctx, "down", "--remove-orphans")


@app.command()
def status(ctx: typer.Context) -> None:
    """Show the status of the stack services."""
    _compose(ctx, "ps", "--all")


@app.command()
def logs(
    ctx: typer.Context,
    service: Annotated[str | None, typer.Argument(help="Service name (default: all).")] = None,
    follow: Annotated[
        bool, typer.Option("--follow/--no-follow", "-f", help="Keep streaming new lines.")
    ] = True,
    tail: Annotated[
        int, typer.Option("--tail", "-n", min=0, help="Lines to show from the end of each log.")
    ] = 100,
) -> None:
    """Show service logs (Ctrl+C stops following; the stack keeps running)."""
    args = ["logs", "--tail", str(tail)]
    if follow:
        args.append("--follow")
    if service:
        args.append(service)
    _compose(ctx, *args)
