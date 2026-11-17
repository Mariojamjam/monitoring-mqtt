"""Alert rules (``config/alerts.yaml``): the shared definition.

The alert engine evaluates them (``consumers.rules``), the CLI validates them from the
experiment, and the API exposes them so the dashboard can draw the thresholds. Every reader
uses ``load_configured_rules``: the rules the CLI passed in ``ALERT_RULES``, else the file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from monitoring_mqtt.core.alerts import Condition, Severity
from monitoring_mqtt.core.topics import SEGMENT_PATTERN

DEFAULT_RULES_PATH = Path("config/alerts.yaml")

Segment = Annotated[str, Field(pattern=SEGMENT_PATTERN, max_length=64)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
Count = Annotated[int, Field(ge=1, le=1000)]


class RulesError(ValueError):
    """The rules file is missing or invalid."""


class Rule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Segment
    device_type: str
    above: Finite | None = None
    below: Finite | None = None
    severity: Severity = Severity.WARNING
    after: Count = 1
    clear_at: Finite | None = None
    clear_after: Count = 1

    @model_validator(mode="after")
    def _one_threshold(self) -> Rule:
        if (self.above is None) == (self.below is None):
            raise ValueError(f"rule {self.id}: set exactly one of above or below")
        if self.clear_at is not None:
            if self.above is not None and self.clear_at > self.above:
                raise ValueError(f"rule {self.id}: clear_at must not be above `above`")
            if self.below is not None and self.clear_at < self.below:
                raise ValueError(f"rule {self.id}: clear_at must not be below `below`")
        return self

    @property
    def condition(self) -> Condition:
        return Condition.ABOVE if self.above is not None else Condition.BELOW

    @property
    def threshold(self) -> float:
        return self.above if self.above is not None else self.below  # type: ignore[return-value]

    @property
    def clear_level(self) -> float:
        return self.threshold if self.clear_at is None else self.clear_at

    def violated(self, value: float) -> bool:
        return value > self.threshold if self.above is not None else value < self.threshold

    def recovered(self, value: float) -> bool:
        level = self.clear_level
        return value <= level if self.above is not None else value >= level


class RulesFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> RulesFile:
        ids = [r.id for r in self.rules]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"duplicated rule ids: {duplicated}")
        return self

    def to_env(self) -> str:
        return self.model_dump_json(exclude_defaults=True)

    @classmethod
    def from_env(cls, value: str) -> RulesFile:
        return cls.model_validate_json(value)

    def device_types(self) -> set[str]:
        return {r.device_type for r in self.rules}


def load_rules(path: Path = DEFAULT_RULES_PATH) -> RulesFile:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RulesError(f"cannot read alert rules {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RulesError(f"invalid YAML in {path}: {exc}") from exc
    try:
        return RulesFile.model_validate(data)
    except ValidationError as exc:
        raise RulesError(f"invalid alert rules {path}: {exc}") from exc


def load_configured_rules() -> RulesFile:
    """``ALERT_RULES`` (JSON, set by the CLI from the experiment) or the rules file."""
    raw = os.environ.get("ALERT_RULES", "").strip()
    if raw:
        try:
            return RulesFile.from_env(raw)
        except ValidationError as exc:
            raise RulesError(f"invalid ALERT_RULES: {exc}") from exc
    return load_rules(Path(os.environ.get("ALERT_RULES_FILE", str(DEFAULT_RULES_PATH))))
