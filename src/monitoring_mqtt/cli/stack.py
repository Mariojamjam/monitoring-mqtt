"""``monitoring-mqtt stack`` commands: manage the Docker Compose stack."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from monitoring_mqtt.cli import compose
from monitoring_mqtt.cli.dockerhost import DockerHost, DockerHostError, inspect_docker_host
from monitoring_mqtt.cli.machine import (
    ALERTS,
    BROKER,
    DASHBOARD,
    DEVICES,
    MONITORS,
    STORAGE,
    Machine,
    MachineError,
    load_machine,
)
from monitoring_mqtt.cli.project import ProjectNotFoundError, resolve_project_root
from monitoring_mqtt.cli.simulation import (
    Simulation,
    SimulationError,
    StackPlan,
    compose_env,
    load_simulation,
    resolve_run_id,
)
from monitoring_mqtt.core.durations import human_duration
from monitoring_mqtt.core.identity import host_short_id
from monitoring_mqtt.core.registry import WorkerClaim

BROKER_SERVICE = "broker"
STORAGE_SERVICES = ("postgres", "api", "ingestion")  # long-running (migrate exits)
ALERTS_SERVICE = "alerts"
DASHBOARD_SERVICE = "dashboard"
RELAY_SERVICE = "broker-relay"
WORKERS_SERVICE = "workers"

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


def load_machine_or_exit(root: Path) -> Machine:
    try:
        return load_machine(root)
    except MachineError as exc:
        err_console.print(f"[red]error:[/red] machine settings (.env): {exc}")
        raise typer.Exit(code=2) from exc


def load_simulation_or_exit(root: Path, config: Path | None) -> Simulation:
    try:
        return load_simulation(root, config)
    except SimulationError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def for_machine(sim: Simulation, machine: Machine) -> Simulation:
    """The experiment as this machine runs it: other machines' roles get 0 replicas."""
    plan = sim.plan.limited_to(
        devices=machine.runs(DEVICES),
        monitors=machine.runs(MONITORS),
        storage=machine.runs(STORAGE),
        alerts=machine.runs(ALERTS),
        dashboard=machine.runs(DASHBOARD),
    )
    return replace(sim, plan=plan)


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
    # Ingestion and alerts use fixed client ids (persistent sessions): exactly one each.
    expected.update({s: int(plan.storage) for s in STORAGE_SERVICES})
    expected[ALERTS_SERVICE] = int(plan.alerts)
    expected[DASHBOARD_SERVICE] = int(plan.dashboard)
    return [
        *_check_counts(expected, running),
        # Device and monitor client ids embed the container's short id; check each group.
        *_check_unique_ids(running, [w.service for w in plan.workers]),
        *_check_unique_ids(running, [m.service for m in plan.monitors]),
    ]


def service_running(root: Path, service: str) -> bool:
    result = compose.capture(root, "ps", "--status", "running", "--quiet", service)
    return result.returncode == 0 and bool(result.stdout.strip())


def relay_listen_address(
    machine: Machine, inspect: Callable[[], DockerHost] = inspect_docker_host
) -> str | None:
    """Where the broker-relay listens, or ``None`` when this machine does not need it."""
    if not machine.tunnel_on_host:
        return None
    try:
        docker_host = inspect()
    except DockerHostError as exc:
        err_console.print(f"[red]error:[/red] cannot inspect Docker: {exc}")
        raise typer.Exit(code=1) from exc
    if not docker_host.needs_relay:
        return None  # Docker Desktop: host.docker.internal reaches the host's loopback
    if not docker_host.bridge_gateway:
        err_console.print(
            "[red]error:[/red] cannot find the Docker bridge gateway for the broker relay."
        )
        raise typer.Exit(code=1)
    return docker_host.bridge_gateway


def broker_env(root: Path, machine: Machine) -> dict[str, str]:
    """Compose variables for one-off services, after checking their broker path is up."""
    relay = relay_listen_address(machine)
    if machine.runs(BROKER) and not service_running(root, BROKER_SERVICE):
        err_console.print(
            "[red]error:[/red] the stack is not running. "
            "Start it with 'monitoring-mqtt stack up' first."
        )
        raise typer.Exit(code=1)
    if relay and not service_running(root, RELAY_SERVICE):
        err_console.print(
            f"[red]error:[/red] {RELAY_SERVICE} is not running. "
            "Start it with 'monitoring-mqtt stack up' first."
        )
        raise typer.Exit(code=1)
    return machine.compose_env(relay)


def parse_claims(output: str) -> list[WorkerClaim]:
    """Claims printed by the workers tool (one JSON object per line; other lines ignored)."""
    claims = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                claims.append(WorkerClaim.model_validate(json.loads(line)))
            except ValueError:
                continue
    return claims


def print_claims(claims: list[WorkerClaim], local_containers: set[str]) -> None:
    table = Table(
        title="Workers registered in the broker",
        caption="here = container on this machine; own id = not derived from the container "
        "(WORKER_ID, or a new id after a collision)",
    )
    table.add_column("Worker id", no_wrap=True)
    table.add_column("Container", no_wrap=True)
    table.add_column("Devices")
    table.add_column("Run id")
    table.add_column("Since", no_wrap=True)
    table.add_column("Notes")
    for claim in claims:
        notes = []
        if claim.hostname[:12] in local_containers:
            notes.append("here")
        if host_short_id(claim.hostname) != claim.worker_id:
            notes.append("own id")
        table.add_row(
            claim.worker_id,
            claim.hostname[:12],
            f"{claim.devices} x {', '.join(claim.device_types)}",
            claim.run_id,
            claim.started_at.strftime("%H:%M:%S"),
            ", ".join(notes) or "-",
        )
    console.print(table)


def _short_ids(running: dict[str, list[str]], service: str) -> str:
    return ", ".join(sorted(host_short_id(cid) for cid in running.get(service, []))) or "-"


def _role_note(machine: Machine, role: str) -> str:
    return "" if machine.runs(role) else f"; role '{role}' not on this machine, so N = 0"


def print_workers(sim: Simulation, machine: Machine, running: dict[str, list[str]]) -> None:
    workers = sim.plan.workers
    table = Table(
        title=f"Publishers ({sim.source.as_posix()}: workers)",
        caption="N = worker containers, M = devices per container; ids claimed in the broker: "
        "'stack workers'" + _role_note(machine, DEVICES),
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


def print_monitors(sim: Simulation, machine: Machine, running: dict[str, list[str]]) -> None:
    monitors = sim.plan.monitors
    table = Table(
        title=f"Subscribers ({sim.source.as_posix()}: monitors)",
        caption="Each replica is an independent subscriber and receives its own copy"
        + _role_note(machine, MONITORS),
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


def print_experiment(
    sim: Simulation, machine: Machine, run_id: str, generated: bool, relay: str | None = None
) -> None:
    mqtt = sim.mqtt
    relay_note = (
        f" via {RELAY_SERVICE} {relay}:{machine.broker_port}"
        if relay
        else (" via host.docker.internal" if machine.tunnel_on_host else "")
    )
    table = Table(title=f"Experiment ({sim.source.as_posix()})", show_header=False)
    table.add_column("Parameter", no_wrap=True)
    table.add_column("Value")
    table.add_row("machine", f"{machine.node} (.env NODE_NAME)")
    table.add_row("machine roles", f"{machine.role_list} (.env STACK_ROLES)")
    table.add_row("broker", machine.describe_broker() + relay_note)
    table.add_row("run id", f"{run_id}{' (generated for this run)' if generated else ''}")
    table.add_row("seed", str(sim.run.seed))
    table.add_row("log level", sim.run.log_level)
    table.add_row("publish interval", f"{sim.devices.publish_interval_seconds:g} s")
    table.add_row("behaviour", sim.behavior.describe())
    if sim.plan.storage:
        table.add_row(
            "storage",
            f"on: API {machine.api_url} (reference {machine.api_url}/docs); batches of "
            f"{sim.storage.batch_size}, every {human_duration(sim.storage.flush_interval)}",
        )
    else:
        table.add_row("storage", "off" + _off_reason(sim.storage.enabled, machine, STORAGE))
    if sim.plan.alerts and sim.rules is not None:
        table.add_row("alerts", f"on: {len(sim.rules.rules)} rules from {sim.alerts.rules}")
    else:
        table.add_row("alerts", "off" + _off_reason(sim.alerts.enabled, machine, ALERTS))
    if sim.plan.dashboard:
        refresh = sim.dashboard.refresh
        live = f"live every {human_duration(refresh)}" if refresh else "live refresh off"
        api = machine.dashboard_api_url or "this stack's API"
        table.add_row("dashboard", f"on: {machine.dashboard_url} ({live}; reads {api})")
    else:
        table.add_row("dashboard", "off" + _off_reason(sim.dashboard.enabled, machine, DASHBOARD))
    for fault in sim.behavior.faults:
        lasts = f" for {human_duration(fault.lasts)}" if fault.lasts else " until the end"
        table.add_row(
            "  fault", f"{fault.action.value} {fault.device} at {human_duration(fault.at)}{lasts}"
        )
    table.add_row("MQTT QoS", str(mqtt.qos))
    table.add_row("MQTT keepalive", f"{mqtt.keepalive_seconds} s")
    table.add_row("MQTT clean session", str(mqtt.clean_session).lower())
    table.add_row(
        "MQTT reconnect",
        f"{mqtt.reconnect.min_delay_seconds}-{mqtt.reconnect.max_delay_seconds} s backoff",
    )
    console.print(table)


def _off_reason(enabled: bool, machine: Machine, role: str) -> str:
    if not enabled:
        return " (disabled in the experiment)"
    return f" here (role '{role}' not on this machine)" if not machine.runs(role) else ""


def print_topology(sim: Simulation, machine: Machine, running: dict[str, list[str]]) -> None:
    plan = sim.plan
    print_workers(sim, machine, running)
    print_monitors(sim, machine, running)
    console.print(
        f"Total: {sum(w.devices for w in plan.workers)} device(s) in "
        f"{sum(w.replicas for w in plan.workers)} worker container(s); "
        f"{sum(m.replicas for m in plan.monitors)} monitor container(s). "
        "One MQTT connection per device and per monitor."
    )


ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="Experiment file to apply (default: config/simulation.yaml).",
        dir_okay=False,
    ),
]


@app.command()
def up(
    ctx: typer.Context,
    build: Annotated[bool, typer.Option("--build", help="Build images before starting.")] = False,
    config: ConfigOption = None,
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Run id for this start (overrides run.run_id)."),
    ] = None,
) -> None:
    """Start this machine's part of the experiment (a new run each time).

    The experiment comes from config/simulation.yaml (or --config); which services run
    here and where the broker is come from the machine settings in .env.
    """
    root = _project_root(ctx)
    machine = load_machine_or_exit(root)
    sim = for_machine(load_simulation_or_exit(root, config), machine)
    try:
        effective_run_id = resolve_run_id(sim, run_id)
    except SimulationError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    generated = not run_id and not sim.run.run_id

    args = ["up", "--detach", "--wait", "--remove-orphans"]
    if not machine.runs(BROKER):
        args.append("--no-deps")  # the broker runs elsewhere
    if build:
        args.append("--build")
    plan = sim.plan
    relay = relay_listen_address(machine)
    env = {**compose_env(sim, effective_run_id), **machine.compose_env(relay)}
    code = _run(root, *args, env=env)
    if code != 0:
        raise typer.Exit(code=code)

    services = [w.service for w in plan.workers] + [m.service for m in plan.monitors]
    services += [*STORAGE_SERVICES, ALERTS_SERVICE, DASHBOARD_SERVICE]
    running = running_containers(root, [*services, RELAY_SERVICE])
    print_experiment(sim, machine, effective_run_id, generated, relay)
    print_topology(sim, machine, running)
    if generated and machine.distributed:
        err_console.print(
            "[yellow]warning:[/yellow] the run id was generated on this machine; other "
            "machines in this experiment will not share it. Set run.run_id or pass --run-id."
        )
    problems = verify_stack(plan, running)
    if relay and len(running.get(RELAY_SERVICE, [])) != 1:
        problems.append(f"{RELAY_SERVICE}: expected 1 container, found 0")
    for problem in problems:
        err_console.print(f"[red]error:[/red] {problem}")
    raise typer.Exit(code=1 if problems else 0)


@app.command()
def workers(ctx: typer.Context) -> None:
    """List the device workers registered in the broker, from every machine."""
    root = _project_root(ctx)
    machine = load_machine_or_exit(root)
    try:
        env = broker_env(root, machine)
        result = compose.capture(root, "run", "--rm", "-T", "--no-deps", WORKERS_SERVICE, env=env)
    except compose.ComposeError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc
    if result.returncode != 0:
        err_console.print(result.stderr.strip())
        err_console.print(
            f"[red]could not read the registry (exit code {result.returncode}).[/red]"
        )
        raise typer.Exit(code=result.returncode)

    claims = parse_claims(result.stdout)
    if not claims:
        console.print(f"No workers registered in broker {machine.describe_broker()}.")
        raise typer.Exit(code=0)
    running = running_containers(root, _worker_services(root))
    local = {cid[:12] for ids in running.values() for cid in ids}
    print_claims(claims, local)
    console.print(f"{len(claims)} worker(s), {sum(c.devices for c in claims)} device(s).")


def _worker_services(root: Path) -> list[str]:
    try:
        return [w.service for w in load_simulation(root).plan.workers]
    except SimulationError:
        return []


@app.command()
def down(
    ctx: typer.Context,
    volumes: Annotated[
        bool, typer.Option("--volumes", help="Also delete the stored history (database).")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Stop and remove the stack containers and network (the history is kept)."""
    if volumes and not yes:
        typer.confirm("Delete the stored history (PostgreSQL volume) too?", abort=True)
    _compose(ctx, "down", "--remove-orphans", *(["--volumes"] if volumes else []))


@app.command()
def restart(
    ctx: typer.Context,
    services: Annotated[
        list[str] | None, typer.Argument(help="Services to restart (default: all).")
    ] = None,
    timeout: Annotated[
        int | None,
        typer.Option("--timeout", "-t", min=0, help="Seconds to wait for a clean stop."),
    ] = None,
) -> None:
    """Restart running containers as they are (same run id and settings).

    Use it to provoke a restart, e.g. 'stack restart broker'. To apply a changed
    config/simulation.yaml or start a new run, use 'stack up'.
    """
    args = ["restart"]
    if timeout is not None:
        args += ["--timeout", str(timeout)]
    _compose(ctx, *args, *(services or []))


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
