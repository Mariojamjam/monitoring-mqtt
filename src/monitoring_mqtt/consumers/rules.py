"""Alert rules (``config/alerts.yaml``) and their evaluation (pure: no MQTT, no clock).

A rule watches one device type: ``above`` or ``below`` a threshold, with a severity.
Each (device, rule) pair has its own state::

    NORMAL ──(`after` violations in a row)──► RAISED: publishes `raised` (new alert_id)
    RAISED ──(`clear_after` values back past `clear_at`)──► NORMAL: publishes `cleared`
                                                              (same alert_id)

- ``after`` avoids alerting on a single noisy value;
- ``clear_at`` adds hysteresis: an alert above 25 that clears at 23 does not flap when the
  value hovers around 25.

Repeated measurements (QoS 1 duplicates) are ignored by ``event_id``; older ones by
``sequence``. A device restart (sequence back to 1, or a new run) restarts the counting.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from monitoring_mqtt.core.alerts import AlertEvent, AlertState, Condition, Severity
from monitoring_mqtt.core.models import Measurement
from monitoring_mqtt.core.topics import SEGMENT_PATTERN

DEFAULT_RULES_PATH = Path("config/alerts.yaml")
SEEN_EVENTS_PER_DEVICE = 64

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


@dataclass
class RuleState:
    open: AlertEvent | None = None  # the raised event of the open alert
    violations: int = 0
    recoveries: int = 0


@dataclass
class _DeviceHistory:
    run_id: str
    sequence: int
    seen: deque[uuid.UUID] = field(default_factory=lambda: deque(maxlen=SEEN_EVENTS_PER_DEVICE))


class AlertEvaluator:
    """Turns measurements into alert transitions."""

    def __init__(self, rules: Iterable[Rule]) -> None:
        self.rules: dict[str, list[Rule]] = {}
        for rule in rules:
            self.rules.setdefault(rule.device_type, []).append(rule)
        self.states: dict[tuple[str, str], RuleState] = {}
        self._devices: dict[str, _DeviceHistory] = {}

    def restore(self, event: AlertEvent) -> None:
        """An alert still open (retained active state), e.g. after a restart."""
        if event.state is AlertState.RAISED:
            self.states[(event.device_id, event.rule_id)] = RuleState(open=event)

    def forget(self, device_id: str, rule_id: str) -> None:
        self.states.pop((device_id, rule_id), None)

    def open_alerts(self) -> list[AlertEvent]:
        return [s.open for s in self.states.values() if s.open is not None]

    def _fresh(self, m: Measurement) -> bool:
        """False for duplicates and stale measurements; resets counting on a restart."""
        history = self._devices.get(m.device_id)
        if history is None or history.run_id != m.run_id or m.sequence == 1:
            restarted = history is not None
            history = self._devices[m.device_id] = _DeviceHistory(m.run_id, m.sequence)
            if restarted:
                for (device_id, _), state in self.states.items():
                    if device_id == m.device_id:
                        state.violations = state.recoveries = 0
            history.seen.append(m.event_id)
            return True
        if m.event_id in history.seen or m.sequence <= history.sequence:
            return False
        history.sequence = m.sequence
        history.seen.append(m.event_id)
        return True

    def evaluate(self, m: Measurement) -> list[AlertEvent]:
        rules = self.rules.get(m.device_type)
        if not rules or not self._fresh(m):
            return []
        events = []
        for rule in rules:
            state = self.states.setdefault((m.device_id, rule.id), RuleState())
            event = self._step(rule, state, m)
            if event is not None:
                events.append(event)
        return events

    def _step(self, rule: Rule, state: RuleState, m: Measurement) -> AlertEvent | None:
        if state.open is None:
            state.violations = state.violations + 1 if rule.violated(m.value) else 0
            if state.violations < rule.after:
                return None
            state.open = _event(rule, m, uuid.uuid4(), AlertState.RAISED)
            state.violations = state.recoveries = 0
            return state.open
        state.recoveries = state.recoveries + 1 if rule.recovered(m.value) else 0
        if state.recoveries < rule.clear_after:
            return None
        cleared = _event(rule, m, state.open.alert_id, AlertState.CLEARED)
        state.open = None
        state.violations = state.recoveries = 0
        return cleared


def _event(rule: Rule, m: Measurement, alert_id: uuid.UUID, state: AlertState) -> AlertEvent:
    return AlertEvent(
        alert_id=alert_id,
        state=state,
        rule_id=rule.id,
        severity=rule.severity,
        condition=rule.condition,
        threshold=rule.threshold,
        device_id=m.device_id,
        device_type=m.device_type,
        domain=m.domain,
        metric=m.metric,
        unit=m.unit,
        run_id=m.run_id,
        value=m.value,
        measurement_event_id=m.event_id,
        measurement_timestamp=m.timestamp,
    )
