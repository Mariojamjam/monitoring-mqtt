"""The ``behavior`` section of an experiment: anomalies, instability and scheduled faults.

Written by people in ``config/simulation.yaml``, validated by the CLI, passed to every
worker as JSON (``SIMULATION_BEHAVIOR``) and validated again there with the same models.

    behavior:
      anomalies:   {every: 2m, lasts: 30s, direction: both, only: [ammonia_detector]}
      instability: {every: 3m, lasts: 20s, kinds: [pause, delay, disconnect]}
      faults:
        - {device: poultry-*-002, at: 1m, lasts: 30s, action: stop}

Durations are written with a unit: ``500ms``, ``30s``, ``2m``, ``1h``.
"""

from __future__ import annotations

import fnmatch
import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)

from monitoring_mqtt.core.simulation import EpisodeKind

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)\s*$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
DEVICE_PATTERN_RE = re.compile(r"^[a-z0-9*?_\-\[\]]+$")


def _seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"write a duration with a unit, e.g. '30s' or '2m' (got {value!r})")
    match = _DURATION_RE.match(value)
    if not match:
        if re.fullmatch(r"\s*\d+(\.\d+)?\s*", value):
            raise ValueError(
                f"add a unit to {value!r}, e.g. '{value.strip()}s' or '{value.strip()}m'"
            )
        raise ValueError(f"invalid duration {value!r}: use e.g. '500ms', '30s', '2m' or '1h'")
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def parse_duration(value: object) -> float:
    """``"30s"`` → 30.0. Bare numbers are rejected: the unit must be explicit."""
    seconds = _seconds(value)
    if seconds <= 0:
        raise ValueError(f"duration {value!r} must be greater than zero")
    return seconds


def parse_offset(value: object) -> float:
    """A time after the start, like a duration but ``0s`` (right away) is allowed."""
    return _seconds(value)


def format_duration(seconds: float) -> str:
    return f"{seconds:g}s"


Duration = Annotated[
    float, BeforeValidator(parse_duration), PlainSerializer(format_duration, return_type=str)
]
Offset = Annotated[
    float, BeforeValidator(parse_offset), PlainSerializer(format_duration, return_type=str)
]


UNSTABLE_KINDS = (EpisodeKind.PAUSE, EpisodeKind.DELAY, EpisodeKind.DISCONNECT)


class Direction(StrEnum):
    HIGH = "high"
    LOW = "low"
    BOTH = "both"


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AnomaliesConfig(_Section):
    every: Duration  # average time between anomalies of one device
    lasts: Duration = 30.0
    direction: Direction = Direction.BOTH
    only: list[str] | None = None  # device types; None = every type with anomaly targets


class InstabilityConfig(_Section):
    every: Duration
    lasts: Duration = 20.0
    kinds: Annotated[list[EpisodeKind], Field(min_length=1)] = list(UNSTABLE_KINDS)
    only: list[str] | None = None

    @field_validator("kinds")
    @classmethod
    def _unstable_only(cls, kinds: list[EpisodeKind]) -> list[EpisodeKind]:
        wrong = [k.value for k in kinds if k not in UNSTABLE_KINDS]
        if wrong:
            raise ValueError(f"kinds must be pause, delay or disconnect (got {wrong})")
        return list(dict.fromkeys(kinds))


class FaultConfig(_Section):
    device: str  # device id; * and ? match any characters
    at: Offset  # time after the worker starts (0s = right away)
    lasts: Duration | None = None  # None: until the end of the run
    action: EpisodeKind
    direction: Literal["high", "low"] = "high"  # only for action: anomaly

    @field_validator("device")
    @classmethod
    def _pattern(cls, device: str) -> str:
        if not DEVICE_PATTERN_RE.match(device):
            raise ValueError(
                f"invalid device {device!r}: a device id, optionally with * or ?, "
                "e.g. poultry-*-002"
            )
        return device

    def matches(self, device_id: str) -> bool:
        return fnmatch.fnmatchcase(device_id, self.device)


class BehaviorConfig(_Section):
    anomalies: AnomaliesConfig | None = None
    instability: InstabilityConfig | None = None
    faults: list[FaultConfig] = Field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.anomalies or self.instability or self.faults)

    def to_env(self) -> str:
        """Compact JSON for the ``SIMULATION_BEHAVIOR`` variable ('' when disabled)."""
        return self.model_dump_json(exclude_defaults=True) if self.enabled else ""

    @classmethod
    def from_env(cls, value: str | None) -> BehaviorConfig:
        return cls.model_validate_json(value) if value and value.strip() else cls()

    def describe(self) -> str:
        """One line for people, e.g. ``anomalies every 2m for 30s (both); 1 fault``."""
        parts = []
        if self.anomalies:
            a = self.anomalies
            parts.append(_episodes("anomalies", a.every, a.lasts, a.direction.value, a.only))
        if self.instability:
            i = self.instability
            kinds = ", ".join(k.value for k in i.kinds)
            parts.append(_episodes("instability", i.every, i.lasts, kinds, i.only))
        if self.faults:
            parts.append(f"{len(self.faults)} fault{'s' if len(self.faults) > 1 else ''}")
        return "; ".join(parts) or "none (normal values, no faults)"

    def device_types(self) -> set[str]:
        """Device type names this behaviour refers to (to check against the catalog)."""
        names: set[str] = set()
        for section in (self.anomalies, self.instability):
            if section and section.only:
                names.update(section.only)
        return names


def human_duration(seconds: float) -> str:
    for unit, size in (("h", 3600.0), ("m", 60.0), ("s", 1.0)):
        if seconds >= size and (seconds / size).is_integer():
            return f"{int(seconds / size)}{unit}"
    return f"{seconds:g}s"


def _episodes(name: str, every: float, lasts: float, what: str, only: list[str] | None) -> str:
    text = f"{name} every {human_duration(every)} for {human_duration(lasts)} ({what})"
    return text + (f" on {', '.join(only)}" if only else "")
