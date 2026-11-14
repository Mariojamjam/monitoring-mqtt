"""Experiment configuration (``config/simulation.yaml``) and its mapping to Compose.

The file defines one experiment: run parameters (run id, seed, log level), MQTT settings
(QoS, keepalive, session, reconnection), device behaviour (publish interval) and the
topology of publishers and subscribers.

Topology:
- each device type has one worker service ``device-{domain}``: N replicas, M devices each;
- each monitor kind has one service ``monitor-{kind}``, where the kind is a device domain
  (filter ``monitoring/v1/{domain}/#``) or ``all`` (filter ``monitoring/v1/#``): N replicas.

The CLI validates the file and passes everything to Compose as variables, for example
``MQTT_QOS=0``, ``DEVICE_AQUACULTURE_REPLICAS=2``, ``MONITOR_POULTRY_REPLICAS=5``.
Machine-specific settings (roles, broker address, host port, smoke timeout) are not part
of this file; they live in the user's ``.env`` (see ``cli/machine.py``). A machine that runs
only some roles applies the same experiment with the other services at 0 replicas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from monitoring_mqtt.core.durations import Duration
from monitoring_mqtt.core.ingest import MAX_BATCH_ITEMS
from monitoring_mqtt.core.rules import RulesError, RulesFile, load_rules
from monitoring_mqtt.core.topics import ALL_TELEMETRY_FILTER, domain_filter
from monitoring_mqtt.simulator.behavior_config import BehaviorConfig
from monitoring_mqtt.simulator.catalog import CatalogError, DeviceCatalog, load_catalog

SIMULATION_FILE = Path("config/simulation.yaml")
CATALOG_FILE = Path("config/devices.yaml")
MAX_REPLICAS = 1_000
MAX_DEVICES_PER_WORKER = 10_000


RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
# The smoke test waits for live telemetry this long: two publish intervals plus a margin.
SMOKE_WINDOW_MARGIN_SECONDS = 5.0


class SimulationError(ValueError):
    """The simulation file is missing, invalid or inconsistent with the catalog."""


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunSection(_Section):
    run_id: Annotated[str, Field(pattern=RUN_ID_PATTERN)] | None = None
    seed: int = 42
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class ReconnectSection(_Section):
    min_delay_seconds: Annotated[int, Field(ge=1, le=3600)] = 1
    max_delay_seconds: Annotated[int, Field(ge=1, le=3600)] = 30

    @model_validator(mode="after")
    def _ordered(self) -> ReconnectSection:
        if self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("reconnect.max_delay_seconds must be >= min_delay_seconds")
        return self


class MqttSection(_Section):
    qos: Literal[0, 1, 2] = 1
    keepalive_seconds: Annotated[int, Field(ge=5, le=3600)] = 30
    clean_session: bool = True
    reconnect: ReconnectSection = Field(default_factory=ReconnectSection)


class DevicesSection(_Section):
    publish_interval_seconds: Annotated[float, Field(ge=0.1, le=86_400, allow_inf_nan=False)] = 5.0


class WorkerGroup(_Section):
    replicas: Annotated[int, Field(ge=0, le=MAX_REPLICAS)]
    devices_per_worker: Annotated[int, Field(ge=1, le=MAX_DEVICES_PER_WORKER)]


class MonitorGroup(_Section):
    replicas: Annotated[int, Field(ge=0, le=MAX_REPLICAS)]


class StorageSection(_Section):
    """History: PostgreSQL, the API and the ingestion consumer (MQTT → API)."""

    enabled: bool = True
    batch_size: Annotated[int, Field(ge=1, le=MAX_BATCH_ITEMS)] = 500
    flush_interval: Duration = 1.0
    buffer_limit: Annotated[int, Field(ge=1, le=10_000_000)] = 50_000


class AlertsSection(_Section):
    """The alert engine and its rules file (relative to the project directory)."""

    enabled: bool = True
    rules: str = "config/alerts.yaml"


class SimulationFile(_Section):
    schema_version: Literal[1]
    run: RunSection = Field(default_factory=RunSection)
    mqtt: MqttSection = Field(default_factory=MqttSection)
    devices: DevicesSection = Field(default_factory=DevicesSection)
    workers: dict[str, WorkerGroup]
    monitors: dict[str, MonitorGroup] = Field(default_factory=dict)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
    storage: StorageSection = Field(default_factory=StorageSection)
    alerts: AlertsSection = Field(default_factory=AlertsSection)


@dataclass(frozen=True)
class ServicePlan:
    """What will run for one device type."""

    device_type: str
    domain: str
    service: str
    replicas: int
    devices_per_worker: int

    @property
    def devices(self) -> int:
        return self.replicas * self.devices_per_worker

    @property
    def env_prefix(self) -> str:
        return self.service.upper().replace("-", "_")


@dataclass(frozen=True)
class MonitorPlan:
    """What will run for one monitor kind."""

    kind: str
    service: str
    topic_filter: str
    replicas: int

    @property
    def env_prefix(self) -> str:
        return self.service.upper().replace("-", "_")


@dataclass(frozen=True)
class StackPlan:
    workers: list[ServicePlan]
    monitors: list[MonitorPlan]
    storage: bool = True  # postgres, migrate, api, ingestion
    alerts: bool = True  # alert engine

    def limited_to(
        self, *, devices: bool, monitors: bool, storage: bool = True, alerts: bool = True
    ) -> StackPlan:
        """The plan for one machine: services of roles it does not run get 0 replicas."""
        return StackPlan(
            [w if devices else replace(w, replicas=0) for w in self.workers],
            [m if monitors else replace(m, replicas=0) for m in self.monitors],
            storage=self.storage and storage,
            alerts=self.alerts and alerts,
        )


@dataclass(frozen=True)
class Simulation:
    """A validated experiment: its parameters and the services it runs."""

    run: RunSection
    mqtt: MqttSection
    devices: DevicesSection
    plan: StackPlan
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    storage: StorageSection = field(default_factory=StorageSection)
    alerts: AlertsSection = field(default_factory=AlertsSection)
    rules: RulesFile | None = None  # loaded when alerts are enabled
    source: Path = SIMULATION_FILE  # shown to the user; relative to the project when possible


GLOBAL_MONITOR = "all"


def worker_service(domain: str) -> str:
    return f"device-{domain}"


def monitor_service(kind: str) -> str:
    return f"monitor-{kind}"


def _read_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SimulationError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SimulationError(f"invalid YAML in {path}: {exc}") from exc


def build_plan(topology: SimulationFile, catalog: DeviceCatalog) -> list[ServicePlan]:
    """One plan per catalog type; types absent from the topology get 0 replicas."""
    known = {t.name: t for t in catalog.device_types}
    unknown = sorted(set(topology.workers) - set(known))
    if unknown:
        raise SimulationError(f"unknown device type(s) {unknown}; valid types: {sorted(known)}")
    plans = []
    for device_type in catalog.device_types:
        group = topology.workers.get(device_type.name)
        plans.append(
            ServicePlan(
                device_type=device_type.name,
                domain=device_type.domain,
                service=worker_service(device_type.domain),
                replicas=group.replicas if group else 0,
                devices_per_worker=group.devices_per_worker if group else 1,
            )
        )
    return plans


def monitor_kinds(catalog: DeviceCatalog) -> list[str]:
    """One monitor kind per catalog domain (catalog order), plus the global monitor."""
    domains = list(dict.fromkeys(t.domain for t in catalog.device_types))
    return [*domains, GLOBAL_MONITOR]


def build_monitor_plan(topology: SimulationFile, catalog: DeviceCatalog) -> list[MonitorPlan]:
    """One plan per monitor kind; kinds absent from the topology get 0 replicas."""
    kinds = monitor_kinds(catalog)
    unknown = sorted(set(topology.monitors) - set(kinds))
    if unknown:
        raise SimulationError(f"unknown monitor kind(s) {unknown}; valid kinds: {kinds}")
    return [
        MonitorPlan(
            kind=kind,
            service=monitor_service(kind),
            topic_filter=ALL_TELEMETRY_FILTER if kind == GLOBAL_MONITOR else domain_filter(kind),
            replicas=topology.monitors[kind].replicas if kind in topology.monitors else 0,
        )
        for kind in kinds
    ]


def check_behavior(behavior: BehaviorConfig, catalog: DeviceCatalog) -> None:
    """Device types named in the behaviour must exist and support what is asked."""
    known = {t.name: t for t in catalog.device_types}
    unknown = sorted(behavior.device_types() - set(known))
    if unknown:
        raise SimulationError(
            f"behavior: unknown device type(s) {unknown}; valid types: {sorted(known)}"
        )
    anomalies = behavior.anomalies
    if anomalies is None:
        return
    names = anomalies.only or list(known)
    if not any(known[name].anomaly for name in names):
        raise SimulationError(
            "behavior.anomalies: none of these device types defines anomaly targets "
            "(anomaly: {low, high} in config/devices.yaml)"
        )


def load_alert_rules(
    project_root: Path, section: AlertsSection, catalog: DeviceCatalog
) -> RulesFile:
    """The rules file, checked against the device catalog."""
    path = Path(section.rules)
    path = path if path.is_absolute() else project_root / path
    try:
        rules = load_rules(path)
    except RulesError as exc:
        raise SimulationError(str(exc)) from exc
    known = {t.name for t in catalog.device_types}
    unknown = sorted(rules.device_types() - known)
    if unknown:
        raise SimulationError(
            f"{section.rules}: unknown device type(s) {unknown}; valid types: {sorted(known)}"
        )
    return rules


def simulation_path(project_root: Path, config: Path | None = None) -> Path:
    """The experiment file: ``config`` (relative to the working directory) or the default."""
    return config.expanduser().resolve() if config else project_root / SIMULATION_FILE


def _display_path(project_root: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return path


def load_simulation(project_root: Path, config: Path | None = None) -> Simulation:
    """Read and validate an experiment file (default config/simulation.yaml)."""
    path = simulation_path(project_root, config)
    try:
        sim = SimulationFile.model_validate(_read_yaml(path))
        catalog = load_catalog(project_root / CATALOG_FILE)
    except ValidationError as exc:
        raise SimulationError(f"invalid simulation config {path}: {exc}") from exc
    except CatalogError as exc:
        raise SimulationError(str(exc)) from exc
    plan = StackPlan(
        build_plan(sim, catalog),
        build_monitor_plan(sim, catalog),
        storage=sim.storage.enabled,
        alerts=sim.alerts.enabled,
    )
    check_behavior(sim.behavior, catalog)
    rules = load_alert_rules(project_root, sim.alerts, catalog) if sim.alerts.enabled else None
    return Simulation(
        run=sim.run,
        mqtt=sim.mqtt,
        devices=sim.devices,
        plan=plan,
        behavior=sim.behavior,
        storage=sim.storage,
        alerts=sim.alerts,
        rules=rules,
        source=_display_path(project_root, path),
    )


def generate_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("run-%Y%m%dT%H%M%SZ")


def validate_run_id(run_id: str) -> str:
    if not re.match(RUN_ID_PATTERN, run_id):
        raise SimulationError(
            f"invalid run id {run_id!r}: use letters, digits, '.', '_' or '-' (max 64)"
        )
    return run_id


def resolve_run_id(
    sim: Simulation, override: str | None = None, *, now: datetime | None = None
) -> str:
    """``--run-id``, else the configured run id, else a new one (one per `stack up`)."""
    if override:
        return validate_run_id(override)
    return sim.run.run_id or generate_run_id(now)


def _number(value: float) -> str:
    return f"{value:g}"


def mqtt_env(sim: Simulation) -> dict[str, str]:
    """MQTT and logging variables shared by every Python service."""
    mqtt = sim.mqtt
    return {
        "LOG_LEVEL": sim.run.log_level,
        "MQTT_QOS": str(mqtt.qos),
        "MQTT_KEEPALIVE": str(mqtt.keepalive_seconds),
        "MQTT_CLEAN_SESSION": "true" if mqtt.clean_session else "false",
        "MQTT_RECONNECT_MIN_DELAY_SECONDS": str(mqtt.reconnect.min_delay_seconds),
        "MQTT_RECONNECT_MAX_DELAY_SECONDS": str(mqtt.reconnect.max_delay_seconds),
    }


def compose_env(sim: Simulation, run_id: str) -> dict[str, str]:
    """Every variable compose.yaml interpolates for `stack up`."""
    env = {
        **mqtt_env(sim),
        "RUN_ID": run_id,
        "SIMULATION_SEED": str(sim.run.seed),
        "PUBLISH_INTERVAL_SECONDS": _number(sim.devices.publish_interval_seconds),
        "SIMULATION_BEHAVIOR": sim.behavior.to_env(),
        "STORAGE_REPLICAS": "1" if sim.plan.storage else "0",
        "INGEST_BATCH_SIZE": str(sim.storage.batch_size),
        "INGEST_FLUSH_INTERVAL_SECONDS": _number(sim.storage.flush_interval),
        "INGEST_BUFFER_LIMIT": str(sim.storage.buffer_limit),
        "ALERTS_REPLICAS": "1" if sim.plan.alerts else "0",
        # The API shows the rules (catalog): none when the alert engine is off.
        "ALERT_RULES": (sim.rules or RulesFile(schema_version=1)).to_env(),
    }
    for worker in sim.plan.workers:
        env[f"{worker.env_prefix}_REPLICAS"] = str(worker.replicas)
        env[f"{worker.env_prefix}_DEVICES_PER_WORKER"] = str(worker.devices_per_worker)
    for monitor in sim.plan.monitors:
        env[f"{monitor.env_prefix}_REPLICAS"] = str(monitor.replicas)
    return env


def smoke_window_seconds(sim: Simulation) -> float:
    """How long the smoke test waits for live telemetry (depends on the experiment)."""
    return 2 * sim.devices.publish_interval_seconds + SMOKE_WINDOW_MARGIN_SECONDS


def smoke_env(sim: Simulation) -> dict[str, str]:
    return {
        **mqtt_env(sim),
        "SMOKE_TELEMETRY_WINDOW_SECONDS": _number(smoke_window_seconds(sim)),
    }
