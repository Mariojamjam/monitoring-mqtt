"""``monitoring-mqtt experiment``: run versioned scenarios and report their results.

For every sweep point and repetition the runner:

1. writes the run's experiment file and starts it with ``stack up`` (a new run id);
2. starts the probe (``experiment-probe`` service), which waits for every device, warms
   up and announces the measured window;
3. during the window, samples CPU and memory of every container (this machine and the
   Docker contexts in ``EXPERIMENT_DOCKER_CONTEXTS``) and runs the scenario's failure
   actions on the clock of the window;
4. collects the probe's measurements, computes the run's metrics and saves them.

The stack keeps running the last experiment afterwards.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from monitoring_mqtt import __version__
from monitoring_mqtt.cli import compose
from monitoring_mqtt.cli.machine import DEVICES, Machine
from monitoring_mqtt.cli.project import ProjectNotFoundError
from monitoring_mqtt.cli.simulation import SimulationError, load_simulation, mqtt_env
from monitoring_mqtt.cli.stack import _project_root, broker_env, for_machine, load_machine_or_exit
from monitoring_mqtt.experiments import metrics
from monitoring_mqtt.experiments.results import (
    MANIFEST,
    RESULTS_DIR,
    load_records,
    save_record,
    write_csv,
    write_json,
)
from monitoring_mqtt.experiments.scenario import (
    Action,
    ActionKind,
    Point,
    Scenario,
    ScenarioError,
    campaign_id,
    experiment_for,
    find_scenario,
    list_scenarios,
    load_scenario,
    run_id_for,
)

PROBE_SERVICE = "experiment-probe"
SAMPLE_INTERVAL_SECONDS = 2.0
EXEMPT_SERVICES = {"migrate", PROBE_SERVICE}  # one-off services that exit by design

app = typer.Typer(help="Run versioned experiments and report their results.", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)


# Docker on this machine and on the experiment's other machines ---------------------------


def docker(context: str | None, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    command = ["docker", *(["--context", context] if context else []), *args]
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return subprocess.CompletedProcess(command, 1, "", str(exc))


@dataclass(frozen=True)
class Container:
    context: str | None
    id: str
    service: str
    name: str


def containers(contexts: list[str | None], all_states: bool = False) -> list[Container]:
    found = []
    label = f"label=com.docker.compose.project={compose.project_name()}"
    for context in contexts:
        args = [
            "ps",
            "--filter",
            label,
            "--format",
            '{{.ID}}\t{{.Label "com.docker.compose.service"}}\t{{.Names}}',
        ]
        if all_states:
            args.insert(1, "-a")
        result = docker(context, *args)
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                found.append(Container(context, *parts))
    return found


_UNITS = {
    "b": 1 / 2**20,
    "kib": 1 / 1024,
    "mib": 1.0,
    "gib": 1024.0,
    "kb": 1e3 / 2**20,
    "mb": 1e6 / 2**20,
    "gb": 1e9 / 2**20,
}


def parse_mib(text: str) -> float:
    match = re.match(r"^\s*([\d.]+)\s*([A-Za-z]+)", text)
    if not match:
        return 0.0
    return float(match.group(1)) * _UNITS.get(match.group(2).lower(), 0.0)


def parse_stats(output: str, services: dict[str, str], t: float) -> list[dict[str, Any]]:
    samples = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or parts[0] not in services:
            continue
        cpu = float(parts[1].strip().rstrip("%") or 0)
        samples.append(
            {
                "t": t,
                "service": services[parts[0]],
                "container": parts[0],
                "cpu_percent": cpu,
                "mem_mib": round(parse_mib(parts[2]), 2),
            }
        )
    return samples


class ResourceSampler(threading.Thread):
    """``docker stats`` of the stack's containers, every few seconds, on every machine."""

    def __init__(self, contexts: list[str | None]) -> None:
        super().__init__(name="resource-sampler", daemon=True)
        self.contexts = contexts
        self.samples: list[dict[str, Any]] = []
        self._halt = threading.Event()  # not `_stop`: threading.Thread uses that name

    def run(self) -> None:
        while not self._halt.is_set():
            started = time.time()
            by_context: dict[str | None, dict[str, str]] = {}
            for c in containers(self.contexts):
                by_context.setdefault(c.context, {})[c.id[:12]] = c.service
            for context, services in by_context.items():
                result = docker(
                    context,
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{.ID}}\t{{.CPUPerc}}\t{{.MemUsage}}",
                    *services,
                    timeout=30,
                )
                ids = {k[:12]: v for k, v in services.items()}
                self.samples += parse_stats(result.stdout, ids, round(started, 3))
            self._halt.wait(max(0.0, SAMPLE_INTERVAL_SECONDS - (time.time() - started)))

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=60)


@dataclass
class ActionRecord:
    action: Action
    started: float = 0.0
    restored: float | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "describe": self.action.describe(),
            "started": self.started,
            "restored": self.restored if self.restored is not None else self.started,
            "detail": self.detail,
        }


@dataclass
class ActionRunner:
    """Runs the scenario's actions at their offsets from the start of the window."""

    actions: list[Action]
    contexts: list[str | None]
    records: list[ActionRecord] = field(default_factory=list)
    _threads: list[threading.Thread] = field(default_factory=list)

    def start(self, window_start: float) -> None:
        for action in self.actions:
            record = ActionRecord(action)
            self.records.append(record)
            thread = threading.Thread(target=self._run, args=(record, window_start), daemon=True)
            thread.start()
            self._threads.append(thread)

    def join(self) -> None:
        for thread in self._threads:
            thread.join(timeout=600)

    def _targets(self, action: Action) -> list[Container]:
        found = sorted(
            (c for c in containers(self.contexts) if c.service == action.service),
            key=lambda c: c.name,
        )
        if action.do is ActionKind.KILL:
            found = [c for c in found if c.name.endswith(f"-{action.index}")]
        return found

    def _run(self, record: ActionRecord, window_start: float) -> None:
        action = record.action
        time.sleep(max(0.0, window_start + action.at - time.time()))
        targets = self._targets(action)
        record.started = time.time()
        if not targets:
            record.detail = f"no running container of {action.service}"
            return
        verb = {ActionKind.RESTART: "restart", ActionKind.STOP: "stop", ActionKind.KILL: "kill"}[
            action.do
        ]
        for context in {c.context for c in targets}:
            ids = [c.id for c in targets if c.context == context]
            docker(context, verb, *ids, timeout=120)
        if action.do is ActionKind.RESTART:
            record.restored = time.time()
        elif action.lasts is not None:
            time.sleep(max(0.0, record.started + action.lasts - time.time()))
            for context in {c.context for c in targets}:
                docker(context, "start", *[c.id for c in targets if c.context == context])
            record.restored = time.time()
        record.detail = ", ".join(c.name for c in targets)


def failed_containers(contexts: list[str | None], acted_on: set[str]) -> list[str]:
    """Containers that exited with an error or were killed for lack of memory (excluding
    one-off services and containers stopped on purpose by an action)."""
    failed = []
    for c in containers(contexts, all_states=True):
        if c.service in EXEMPT_SERVICES:
            continue
        state = docker(
            c.context,
            "inspect",
            "-f",
            "{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}",
            c.id,
        ).stdout
        parts = state.split()
        if len(parts) != 3:
            continue
        status, code, oom = parts
        if oom == "true" or (status == "exited" and code != "0" and c.service not in acted_on):
            failed.append(c.name)
    return failed


# One run -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedRun:
    point: Point
    repetition: int
    run_id: str
    config: Path
    devices: int


def git_revision(root: Path) -> str:
    rev = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return f"{rev}{'-dirty' if dirty else ''}" if rev else "unknown"


def plan_runs(
    root: Path, scenario: Scenario, machine: Machine, campaign: Path, name: str
) -> list[PlannedRun]:
    """Write and validate every run's experiment file before starting anything."""
    base_path = root / scenario.base
    try:
        base = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ScenarioError(f"cannot read base experiment {scenario.base}: {exc}") from exc
    planned = []
    for point in scenario.points():
        for repetition in range(1, scenario.repetitions + 1):
            run_id = run_id_for(scenario, name, point, repetition)
            config = campaign / "runs" / run_id / "experiment.yaml"
            config.parent.mkdir(parents=True, exist_ok=True)
            document = experiment_for(scenario, base, point, run_id)
            config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            try:
                sim = for_machine(load_simulation(root, config), machine)
            except SimulationError as exc:
                raise ScenarioError(f"point {point.index} ({point.label}): {exc}") from exc
            devices = sum(w.devices for w in sim.plan.workers)
            planned.append(PlannedRun(point, repetition, run_id, config, devices))
    return planned


def stack_up(root: Path, run: PlannedRun, log: Path) -> int:
    command = [
        sys.executable,
        "-m",
        "monitoring_mqtt.cli.main",
        "--project-dir",
        str(root),
        "stack",
        "up",
        "--config",
        str(run.config),
        "--run-id",
        run.run_id,
    ]
    with log.open("w", encoding="utf-8") as handle:
        env = {**os.environ, "COLUMNS": "120", "NO_COLOR": "1"}
        return subprocess.run(
            command, stdout=handle, stderr=subprocess.STDOUT, check=False, env=env
        ).returncode


def probe(
    root: Path,
    machine: Machine,
    scenario: Scenario,
    run: PlannedRun,
    contexts: list[str | None],
    folder: Path,
) -> tuple[dict | None, list, list, str]:
    """Run the probe and, on its clock, the sampler and the actions.

    Returns (result, resource samples, action records, error)."""
    sim = load_simulation(root, run.config)
    env = {
        **os.environ,
        **mqtt_env(sim),
        **broker_env(root, machine),
        "PROBE_RUN_ID": run.run_id,
        "PROBE_EXPECTED_DEVICES": str(run.devices),
        "PROBE_WARMUP_SECONDS": f"{scenario.warmup:g}",
        "PROBE_DURATION_SECONDS": f"{scenario.duration:g}",
        "PROBE_OBSERVE_TELEMETRY": "true" if scenario.observes_telemetry else "false",
    }
    command = compose.build_command(root, "run", "--rm", "--no-deps", "-T", PROBE_SERVICE)
    sampler = ResourceSampler(contexts)
    actions = ActionRunner(list(scenario.actions), contexts)
    result: dict | None = None
    error = ""
    with (folder / "probe.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, text=True, env=env)
        assert process.stdout is not None
        for line in process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = message.get("event")
            if event == "ready":
                console.print(
                    f"    {message['devices']} devices reporting; warming up {scenario.warmup:g}s"
                )
            elif event == "window_start":
                start = metrics.parse_time(message["at"])
                sampler.start()
                actions.start(start)
                console.print(
                    f"    measuring {scenario.duration:g}s"
                    + (f" with {len(scenario.actions)} action(s)" if scenario.actions else "")
                )
            elif event == "window_end":
                sampler.stop()
                console.print("    window closed; waiting until everything is stored")
            elif event == "result":
                result = message
            elif event == "error":
                error = message.get("reason", "probe error")
        code = process.wait()
    if sampler.is_alive():
        sampler.stop()
    actions.join()
    if result is None and not error:
        error = f"the probe exited with code {code} (see probe.log)"
    return result, sampler.samples, actions.records, error


def execute(
    root: Path, machine: Machine, scenario: Scenario, run: PlannedRun, contexts: list[str | None]
) -> dict[str, Any]:
    folder = run.config.parent
    record: dict[str, Any] = {
        "scenario": scenario.name,
        "point": run.point.index,
        "label": run.point.label,
        "repetition": run.repetition,
        "run_id": run.run_id,
        **{key: _scalar(value) for key, value in run.point.values.items()},
        "devices_planned": run.devices,
    }
    code = stack_up(root, run, folder / "stack-up.log")
    if code != 0:
        return {**record, "status": "error", "error": f"stack up exited with {code}"}
    result, resources, actions, error = probe(root, machine, scenario, run, contexts, folder)
    write_csv(folder / "resources.csv", resources)
    if result is None:
        return {**record, "status": "error", "error": error, "probe_error": True}
    write_json(folder / "result.json", result)
    record.update(
        metrics.run_record(
            result,
            duration=scenario.duration,
            resources=resources,
            actions=[a.as_dict() for a in actions],
        )
    )
    acted_on = {a.action.service for a in actions}
    failed = failed_containers(contexts, acted_on)
    record["containers_failed"] = ", ".join(failed) or None
    record["status"] = "ok" if result.get("settled") or result.get("window") else "unsettled"
    return record


def _scalar(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"replicas", "devices_per_worker"}:
            return f"{value['replicas']}x{value['devices_per_worker']}"
        return json.dumps(value, sort_keys=True)
    return value


# Commands ----------------------------------------------------------------------------------


@app.command("list")
def list_command(ctx: typer.Context) -> None:
    """List the scenarios in experiments/."""
    root = _project_root(ctx)
    table = Table(title="Scenarios (experiments/)")
    for column in ("Name", "Runs", "Window", "Description"):
        table.add_column(column)
    for path, scenario in list_scenarios(root):
        if isinstance(scenario, ScenarioError):
            table.add_row(path.stem, "-", "-", f"[red]invalid:[/red] {scenario}")
            continue
        runs = len(scenario.points()) * scenario.repetitions
        window = f"{scenario.warmup:g}s + {scenario.duration:g}s"
        table.add_row(scenario.name, str(runs), window, scenario.description)
    console.print(table)


@app.command()
def run(
    ctx: typer.Context,
    scenario_name: Annotated[
        str,
        typer.Argument(
            metavar="SCENARIO", help="Name in experiments/ (e.g. qos) or a scenario file."
        ),
    ],
    repetitions: Annotated[
        int | None,
        typer.Option("--repetitions", "-r", min=1, help="Override the scenario's repetitions."),
    ] = None,
    duration: Annotated[
        str | None, typer.Option(help="Override the measured window, e.g. 30s.")
    ] = None,
    warmup: Annotated[str | None, typer.Option(help="Override the warm-up, e.g. 10s.")] = None,
    resume: Annotated[
        Path | None,
        typer.Option(
            help="Continue an interrupted campaign (its results folder).", file_okay=False
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate and list the runs without starting anything."),
    ] = False,
) -> None:
    """Run a scenario: one stack per sweep point and repetition, measured by the probe."""
    root = _project_root(ctx)
    machine = load_machine_or_exit(root)
    try:
        scenario = load_scenario(find_scenario(root, scenario_name))
        updates: dict[str, Any] = {}
        if repetitions is not None:
            updates["repetitions"] = repetitions
        if duration is not None:
            updates["duration"] = duration
        if warmup is not None:
            updates["warmup"] = warmup
        scenario = Scenario.model_validate({**scenario.model_dump(by_alias=True), **updates})
    except (ScenarioError, ValueError) as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    if not machine.runs(DEVICES):
        err_console.print(
            "[red]error:[/red] experiments run from a machine with the 'devices' "
            "role (.env STACK_ROLES); the other machines keep their part running."
        )
        raise typer.Exit(code=2)

    name = resume.name if resume else campaign_id()
    campaign = resume or (root / RESULTS_DIR / scenario.name / name)
    if dry_run:  # validate every run's experiment file, leaving nothing behind
        with tempfile.TemporaryDirectory() as scratch:
            planned = _plan_or_exit(root, scenario, machine, Path(scratch), name)
            print_plan(scenario, planned, campaign, root)
        raise typer.Exit(code=0)
    planned = _plan_or_exit(root, scenario, machine, campaign, name)
    print_plan(scenario, planned, campaign, root)

    contexts: list[str | None] = [None, *machine.experiment_contexts]
    manifest_path = campaign / MANIFEST
    manifest = {
        "scenario": scenario.model_dump(mode="json", by_alias=True),
        "campaign": name,
        "git": git_revision(root),
        "version": __version__,
        "machine": {
            "node": machine.node,
            "roles": machine.role_list,
            "broker": machine.describe_broker(),
            "contexts": machine.experiment_contexts,
        },
        "started_at": datetime.now(UTC).isoformat(),
    }
    if not manifest_path.is_file():
        write_json(manifest_path, manifest)
    (campaign / "scenario.yaml").write_text(
        yaml.safe_dump(scenario.model_dump(mode="json", by_alias=True), sort_keys=False),
        encoding="utf-8",
    )

    done = {r["run_id"]: r for r in load_records(campaign) if r.get("status") in ("ok", "skipped")}
    saturated: list[str] = []
    for point in scenario.points():
        point_runs = [r for r in planned if r.point.index == point.index]
        if saturated:
            for r in point_runs:
                save_record(
                    campaign,
                    {
                        "scenario": scenario.name,
                        "point": point.index,
                        "label": point.label,
                        "repetition": r.repetition,
                        "run_id": r.run_id,
                        "status": "skipped",
                        "error": "a previous point saturated",
                    },
                )
            continue
        records = []
        for r in point_runs:
            if r.run_id in done:
                console.print(f"[dim]  {r.run_id}: already measured[/dim]")
                records.append(done[r.run_id])
                continue
            console.print(
                f"[bold]{r.run_id}[/bold]  {point.label}  "
                f"({r.devices} devices, repetition {r.repetition})"
            )
            try:
                record = execute(root, machine, scenario, r, contexts)
            except (compose.ComposeError, ProjectNotFoundError) as exc:
                record = {"run_id": r.run_id, "status": "error", "error": str(exc)}
            save_record(campaign, record)
            records.append(record)
            print_record(record)
        if scenario.stop_when_saturated:
            reasons = metrics.saturation_reasons(
                records, scenario.saturation.min_delivery, scenario.saturation.max_p95_ms
            )
            if reasons:
                saturated = reasons
                console.print(
                    f"[yellow]Point {point.index} ({point.label}) saturated:[/yellow] "
                    + "; ".join(reasons)
                    + ". Larger points are skipped."
                )
    manifest = {
        **json.loads(manifest_path.read_text()),
        "finished_at": datetime.now(UTC).isoformat(),
        "saturated": saturated or None,
    }
    write_json(manifest_path, manifest)
    relative = _relative(campaign, root)
    console.print(
        f"\nResults: {relative}/runs.csv\nReport:  monitoring-mqtt experiment report "
        f"{relative}\nThe stack still runs the last experiment; 'monitoring-mqtt "
        "stack up' returns to config/simulation.yaml."
    )


def _plan_or_exit(
    root: Path, scenario: Scenario, machine: Machine, campaign: Path, name: str
) -> list[PlannedRun]:
    try:
        return plan_runs(root, scenario, machine, campaign, name)
    except ScenarioError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def print_plan(scenario: Scenario, planned: list[PlannedRun], campaign: Path, root: Path) -> None:
    table = Table(title=f"Scenario {scenario.name}: {len(planned)} run(s)")
    for column in ("Point", "Parameters", "Devices here", "Repetitions"):
        table.add_column(column)
    for point in scenario.points():
        runs = [r for r in planned if r.point.index == point.index]
        table.add_row(str(point.index), point.label, str(runs[0].devices), str(len(runs)))
    console.print(table)
    per_run = scenario.warmup + scenario.duration
    console.print(
        f"Each run: stack up, warm-up {scenario.warmup:g}s, window "
        f"{scenario.duration:g}s, settle. At least {len(planned) * per_run / 60:.0f} "
        f"min in total. Results: {_relative(campaign, root)}"
    )


def print_record(record: dict[str, Any]) -> None:
    if record.get("status") not in ("ok", "unsettled"):
        console.print(f"    [red]{record.get('status')}:[/red] {record.get('error')}")
        return
    parts = [
        f"published {record.get('published')}",
        f"stored {record.get('stored')}",
        f"delivery {record.get('storage_delivery')}",
        f"p95 {record.get('latency_storage_p95_ms')} ms",
    ]
    if record.get("monitor_duplicates"):
        parts.append(f"duplicates {record['monitor_duplicates']}")
    if record.get("containers_failed"):
        parts.append(f"[red]failed: {record['containers_failed']}[/red]")
    console.print("    " + ", ".join(parts))


@app.command()
def report(
    ctx: typer.Context,
    campaign: Annotated[
        Path,
        typer.Argument(
            help="A results folder (results/<scenario>/<id>).", file_okay=False, exists=True
        ),
    ],
) -> None:
    """Aggregate a campaign: summary.csv, charts and report.md in its folder."""
    root = _project_root(ctx)
    command = [
        "uv",
        "run",
        "--frozen",
        "python",
        "-m",
        "monitoring_mqtt.experiments.report",
        str(campaign.resolve()),
    ]
    try:
        code = subprocess.run(command, cwd=root, check=False).returncode
    except FileNotFoundError as exc:
        err_console.print("[red]error:[/red] uv not found; run 'monitoring-mqtt doctor'.")
        raise typer.Exit(code=127) from exc
    raise typer.Exit(code=code)
