"""Scenario files (``experiments/*.yaml``): what varies in an experiment and how to measure.

A scenario starts from an experiment file (``base``), applies fixed overrides (``set``)
and runs one stack per combination of the ``sweep`` values, ``repetitions`` times each::

    name: qos
    base: config/simulation.yaml
    warmup: 30s
    duration: 2m
    repetitions: 3
    sweep:
      mqtt.qos: [0, 1]
    set:
      devices.publish_interval_seconds: 1

Keys are dotted paths into the experiment file; a value replaces what is there. Failures
are a timeline of ``actions`` relative to the start of the measured window.
"""

from __future__ import annotations

import copy
import itertools
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from monitoring_mqtt.core.durations import Duration, Offset

SCENARIOS_DIR = Path("experiments")
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,23}$"
PATH_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_*-]+)*$")


class ScenarioError(ValueError):
    """The scenario file is missing or invalid."""


class ActionKind(StrEnum):
    RESTART = "restart"  # docker restart: every container of the service
    STOP = "stop"  # docker stop, then start again after `for` (if given)
    KILL = "kill"  # docker kill one container (a crash), then start it after `for`


class Action(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    at: Offset  # from the start of the measured window
    do: ActionKind
    service: Annotated[str, Field(pattern=r"^[a-z0-9-]+$")]
    lasts: Offset | None = Field(None, alias="for")  # stop/kill: start again after this
    index: Annotated[int, Field(ge=1)] = 1  # kill: which replica

    @model_validator(mode="after")
    def _consistent(self) -> Action:
        if self.do is ActionKind.RESTART and self.lasts is not None:
            raise ValueError("restart takes no `for`")
        return self

    def describe(self) -> str:
        what = f"{self.do.value} {self.service}"
        if self.do is ActionKind.KILL:
            what += f" (replica {self.index})"
        return what + (f" for {self.lasts:g}s" if self.lasts is not None else "")


class Saturation(BaseModel):
    """When a sweep point counts as saturated (the system no longer keeps up)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_delivery: Annotated[float, Field(gt=0, le=1)] = 0.99
    max_p95_ms: Annotated[float, Field(gt=0)] = 5000.0


class ReportSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    x: str | None = None  # column on the x axis (default: the first sweep key)
    charts: list[str] = Field(default_factory=lambda: ["throughput", "latency", "delivery"])


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    name: Annotated[str, Field(pattern=NAME_PATTERN)]
    description: str = ""
    base: str = "config/simulation.yaml"
    warmup: Offset = 30.0
    duration: Duration = 120.0
    repetitions: Annotated[int, Field(ge=1, le=50)] = 3
    sweep: dict[str, list[Any]] = Field(default_factory=dict)
    set: dict[str, Any] = Field(default_factory=dict)
    actions: list[Action] = Field(default_factory=list)
    observe_telemetry: bool | None = None  # default: on when there are actions
    stop_when_saturated: bool = False
    saturation: Saturation = Field(default_factory=Saturation)
    report: ReportSpec = Field(default_factory=ReportSpec)

    @field_validator("sweep", "set")
    @classmethod
    def _paths(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key in value:
            if not PATH_PATTERN.match(key):
                raise ValueError(f"invalid key {key!r}: use a dotted path like mqtt.qos")
        return value

    @field_validator("sweep")
    @classmethod
    def _non_empty(cls, value: dict[str, list[Any]]) -> dict[str, list[Any]]:
        for key, values in value.items():
            if not values:
                raise ValueError(f"sweep {key}: give at least one value")
        return value

    @model_validator(mode="after")
    def _actions_inside_window(self) -> Scenario:
        for action in self.actions:
            if action.at >= self.duration:
                raise ValueError(f"action at {action.at:g}s is after the window ends")
        return self

    @property
    def observes_telemetry(self) -> bool:
        return bool(self.actions) if self.observe_telemetry is None else self.observe_telemetry

    @property
    def x_key(self) -> str | None:
        return self.report.x or next(iter(self.sweep), None)

    def points(self) -> list[Point]:
        """Every combination of the sweep values, in file order (the first key varies
        slowest), each with a short label."""
        keys = list(self.sweep)
        combos = itertools.product(*(self.sweep[k] for k in keys)) if keys else [()]
        return [
            Point(i, dict(zip(keys, values, strict=True)))
            for i, values in enumerate(combos, start=1)
        ]


@dataclass(frozen=True)
class Point:
    index: int
    values: dict[str, Any]

    @property
    def label(self) -> str:
        if not self.values:
            return "base"
        return ", ".join(f"{key}={_short(value)}" for key, value in self.values.items())


def _short(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_short(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_short(v) for v in value) + "]"
    return str(value)


def set_path(document: dict[str, Any], path: str, value: Any) -> None:
    """Set ``a.b.c`` in nested dictionaries, creating missing levels."""
    *parents, leaf = path.split(".")
    node = document
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[leaf] = copy.deepcopy(value)


def experiment_for(
    scenario: Scenario, base: dict[str, Any], point: Point, run_id: str
) -> dict[str, Any]:
    """The experiment file of one run: the base, ``set``, then the point's values."""
    document = copy.deepcopy(base)
    for path, value in [*scenario.set.items(), *point.values.items()]:
        set_path(document, path, value)
    set_path(document, "run.run_id", run_id)
    return document


def campaign_id(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def run_id_for(scenario: Scenario, campaign: str, point: Point, repetition: int) -> str:
    """Unique and readable: ``qos-20261116T140000Z-p2-r1`` (the history keeps every run)."""
    return f"{scenario.name}-{campaign}-p{point.index}-r{repetition}"


def load_scenario(path: Path) -> Scenario:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScenarioError(f"cannot read scenario {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ScenarioError(f"invalid YAML in {path}: {exc}") from exc
    try:
        return Scenario.model_validate(data)
    except ValidationError as exc:
        raise ScenarioError(f"invalid scenario {path}: {exc}") from exc


def find_scenario(project_root: Path, name_or_path: str) -> Path:
    """``qos`` → ``experiments/qos.yaml``; a path is used as given."""
    candidate = Path(name_or_path)
    if candidate.suffix in (".yaml", ".yml"):
        return candidate if candidate.is_absolute() else Path.cwd() / candidate
    return project_root / SCENARIOS_DIR / f"{name_or_path}.yaml"


def list_scenarios(project_root: Path) -> list[tuple[Path, Scenario | ScenarioError]]:
    found: list[tuple[Path, Scenario | ScenarioError]] = []
    for path in sorted((project_root / SCENARIOS_DIR).glob("*.yaml")):
        try:
            found.append((path, load_scenario(path)))
        except ScenarioError as exc:
            found.append((path, exc))
    return found
