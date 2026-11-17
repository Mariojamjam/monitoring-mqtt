import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from monitoring_mqtt.consumers.rules import AlertEvaluator
from monitoring_mqtt.core.alerts import (
    AlertEvent,
    AlertState,
    Condition,
    Severity,
    active_alert_topic,
    alert_event_topic,
)
from monitoring_mqtt.core.models import Measurement
from monitoring_mqtt.core.rules import Rule, RulesError, RulesFile, load_rules

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
AMMONIA_HIGH = Rule(id="ammonia-high", device_type="ammonia_detector", above=25.0, after=2,
                    clear_at=23.0, clear_after=2)  # fmt: skip
AMMONIA_CRITICAL = Rule(id="ammonia-critical", device_type="ammonia_detector", above=35.0,
                        severity=Severity.CRITICAL)  # fmt: skip


class Device:
    """Builds consecutive measurements of one ammonia detector."""

    def __init__(self, device_id: str = "poultry-abc123-001", run_id: str = "run-1") -> None:
        self.device_id, self.run_id = device_id, run_id
        self.sequence = itertools.count(1)

    def at(self, value: float, sequence: int | None = None) -> Measurement:
        seq = next(self.sequence) if sequence is None else sequence
        return Measurement.create(
            run_id=self.run_id, device_id=self.device_id, device_type="ammonia_detector",
            domain="poultry", site_id="house-01", metric="ammonia", value=value, unit="ppm",
            sequence=seq, timestamp=T0 + timedelta(seconds=5 * seq),
        )  # fmt: skip


def run(evaluator: AlertEvaluator, measurements) -> list[tuple[str, str, float]]:
    return [
        (e.rule_id, e.state.value, e.value) for m in measurements for e in evaluator.evaluate(m)
    ]


def test_raised_after_consecutive_violations_and_cleared_with_hysteresis() -> None:
    evaluator, d = AlertEvaluator([AMMONIA_HIGH]), Device()
    values = [12, 26, 12, 26, 27, 24, 24, 22, 22, 26]
    assert run(evaluator, [d.at(v) for v in values]) == [
        ("ammonia-high", "raised", 27),  # 26 alone was noise; 26, 27 in a row raise it
        ("ammonia-high", "cleared", 22),  # 24 is below 25 but above clear_at 23: still open
    ]


def test_cleared_alert_keeps_the_alert_id() -> None:
    evaluator, d = AlertEvaluator([AMMONIA_CRITICAL]), Device()
    raised, cleared = (evaluator.evaluate(d.at(v)) for v in (40, 20))
    assert raised[0].alert_id == cleared[0].alert_id
    assert raised[0].measurement_event_id != cleared[0].measurement_event_id
    assert evaluator.open_alerts() == []


def test_rules_and_devices_are_independent() -> None:
    evaluator = AlertEvaluator([AMMONIA_HIGH, AMMONIA_CRITICAL])
    a, b = Device("poultry-abc123-001"), Device("poultry-abc123-002")
    events = run(evaluator, [a.at(40), b.at(12), a.at(40)])
    assert events == [("ammonia-critical", "raised", 40), ("ammonia-high", "raised", 40)]
    assert {e.device_id for e in evaluator.open_alerts()} == {"poultry-abc123-001"}


def test_duplicates_and_stale_measurements_are_ignored() -> None:
    evaluator, d = AlertEvaluator([AMMONIA_HIGH]), Device()
    first = d.at(30)
    assert evaluator.evaluate(first) == []
    assert evaluator.evaluate(first) == []  # QoS 1 duplicate: not a second violation
    second, third = d.at(12), d.at(12)
    evaluator.evaluate(third)
    assert evaluator.evaluate(second) == []  # older than the latest one: ignored
    assert run(evaluator, [d.at(30), d.at(30)]) == [("ammonia-high", "raised", 30)]


def test_restart_resets_counting() -> None:
    evaluator, d = AlertEvaluator([AMMONIA_HIGH]), Device()
    evaluator.evaluate(d.at(30))  # one violation
    restarted = Device()  # sequence 1 again
    assert evaluator.evaluate(restarted.at(30)) == []  # counting started over
    assert run(evaluator, [restarted.at(30)]) == [("ammonia-high", "raised", 30)]


def test_restored_alert_is_not_raised_again() -> None:
    evaluator, d = AlertEvaluator([AMMONIA_CRITICAL]), Device()
    (open_alert,) = evaluator.evaluate(d.at(40))
    after_restart = AlertEvaluator([AMMONIA_CRITICAL])
    after_restart.restore(open_alert)
    assert run(after_restart, [d.at(41)]) == []  # still open: nothing new
    (cleared,) = after_restart.evaluate(d.at(20))
    assert cleared.alert_id == open_alert.alert_id and cleared.state is AlertState.CLEARED


@pytest.mark.parametrize(
    "rule",
    [
        {"id": "x", "device_type": "t"},  # no threshold
        {"id": "x", "device_type": "t", "above": 1, "below": 0},  # both
        {"id": "x", "device_type": "t", "above": 25, "clear_at": 26},  # wrong side
        {"id": "x", "device_type": "t", "below": 5, "clear_at": 4},
        {"id": "X Y", "device_type": "t", "above": 1},  # not a topic segment
        {"id": "x", "device_type": "t", "above": 1, "after": 0},
    ],
)
def test_invalid_rules(rule: dict) -> None:
    with pytest.raises(ValueError):
        Rule.model_validate(rule)


def test_rules_file(tmp_path: Path) -> None:
    shipped = load_rules(Path(__file__).parents[2] / "config" / "alerts.yaml")
    assert RulesFile.from_env(shipped.to_env()) == shipped
    duplicated = {"schema_version": 1, "rules": [AMMONIA_HIGH.model_dump(mode="json")] * 2}
    path = tmp_path / "alerts.yaml"
    path.write_text(yaml.safe_dump(duplicated))
    with pytest.raises(RulesError, match="duplicated"):
        load_rules(path)
    with pytest.raises(RulesError, match="cannot read"):
        load_rules(tmp_path / "missing.yaml")


def test_alert_contract() -> None:
    (event,) = AlertEvaluator([AMMONIA_CRITICAL]).evaluate(Device().at(40))
    assert (event.condition, event.threshold, event.unit) == (Condition.ABOVE, 35.0, "ppm")
    assert event.topic == alert_event_topic("poultry", "critical", "poultry-abc123-001")
    assert event.topic == "monitoring/alerts/v1/events/poultry/critical/poultry-abc123-001"
    assert event.active_topic == active_alert_topic("poultry-abc123-001", "ammonia-critical")
    assert AlertEvent.from_payload(event.to_payload()) == event
