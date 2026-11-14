"""Evaluation of the alert rules (``core.rules``); pure: no MQTT, no clock.

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

from monitoring_mqtt.core.alerts import AlertEvent, AlertState
from monitoring_mqtt.core.models import Measurement
from monitoring_mqtt.core.rules import Rule

SEEN_EVENTS_PER_DEVICE = 64


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
