import json
from pathlib import Path

import pytest
import yaml

from monitoring_mqtt.experiments import metrics
from monitoring_mqtt.experiments.scenario import (
    Scenario,
    ScenarioError,
    experiment_for,
    list_scenarios,
    load_scenario,
    run_id_for,
    set_path,
)

REPO = Path(__file__).resolve().parents[2]


def scenario(**fields) -> Scenario:
    return Scenario.model_validate({"schema_version": 1, "name": "demo", **fields})


# Scenarios ---------------------------------------------------------------------------------


def test_sweep_expands_in_file_order() -> None:
    s = scenario(sweep={"mqtt.qos": [0, 1], "devices.publish_interval_seconds": [1, 0.5]})
    points = s.points()
    assert [p.values for p in points] == [
        {"mqtt.qos": 0, "devices.publish_interval_seconds": 1},
        {"mqtt.qos": 0, "devices.publish_interval_seconds": 0.5},
        {"mqtt.qos": 1, "devices.publish_interval_seconds": 1},
        {"mqtt.qos": 1, "devices.publish_interval_seconds": 0.5},
    ]
    assert points[1].label == "mqtt.qos=0, devices.publish_interval_seconds=0.5"
    assert [p.label for p in scenario().points()] == ["base"]  # no sweep: one point


def test_experiment_file_of_a_run() -> None:
    s = scenario(
        set={"run.log_level": "WARNING", "workers.ammonia_detector.replicas": 0},
        sweep={"mqtt.qos": [0]},
    )
    base = {
        "run": {"seed": 1},
        "workers": {"ammonia_detector": {"replicas": 1, "devices_per_worker": 2}},
    }
    point = s.points()[0]
    run_id = run_id_for(s, "20261116T000000Z", point, 2)
    document = experiment_for(s, base, point, run_id)
    assert document["run"] == {"seed": 1, "log_level": "WARNING", "run_id": run_id}
    assert document["workers"]["ammonia_detector"] == {"replicas": 0, "devices_per_worker": 2}
    assert document["mqtt"] == {"qos": 0}
    assert base["run"] == {"seed": 1}  # the base is not modified
    assert run_id == "demo-20261116T000000Z-p1-r2"
    nested: dict = {}
    set_path(nested, "a.b.c", [1])
    assert nested == {"a": {"b": {"c": [1]}}}


@pytest.mark.parametrize(
    "fields",
    [
        {"sweep": {"bad key!": [1]}},
        {"sweep": {"mqtt.qos": []}},
        {"duration": "30s", "actions": [{"at": "40s", "do": "restart", "service": "broker"}]},
        {"actions": [{"at": "1s", "do": "restart", "service": "broker", "for": "5s"}]},
        {"duration": "30"},  # durations need a unit
    ],
)
def test_invalid_scenarios(fields) -> None:
    with pytest.raises(ValueError):
        scenario(**fields)


def test_shipped_scenarios_are_valid() -> None:
    found = list_scenarios(REPO)
    assert {p.stem for p, _ in found} >= {"quick", "scale", "qos", "broker-failure"}
    assert not [p for p, s in found if isinstance(s, ScenarioError)]
    for path, s in found:
        assert s.name == path.stem  # type: ignore[union-attr]
    with pytest.raises(ScenarioError):
        load_scenario(REPO / "experiments" / "missing.yaml")


def test_every_shipped_run_is_a_valid_experiment(tmp_path: Path) -> None:
    from monitoring_mqtt.cli.experiment import plan_runs
    from monitoring_mqtt.cli.machine import machine_from_env

    machine = machine_from_env({})
    for _, s in list_scenarios(REPO):
        planned = plan_runs(REPO, s, machine, tmp_path / s.name, "c")  # type: ignore[arg-type]
        assert planned and all(r.devices > 0 for r in planned)
    scale = plan_runs(
        REPO, load_scenario(REPO / "experiments" / "scale.yaml"), machine, tmp_path / "s", "c"
    )
    assert [r.devices for r in scale if r.repetition == 1] == [3, 50, 100, 250, 500]


# Metrics -----------------------------------------------------------------------------------


def snapshot(
    devices: dict[str, tuple[int, int, int, int]],
    monitors: dict[str, tuple[str, int, int]] | None = None,
) -> dict:
    return {
        "workers": {
            "w1": {
                "devices": {
                    d: dict(zip(metrics.PUBLISHER_FIELDS, v, strict=True))
                    for d, v in devices.items()
                }
            }
        },
        "monitors": {
            c: {"filter": f, "received": r, "duplicates": dup}
            for c, (f, r, dup) in (monitors or {}).items()
        },
    }


def test_publisher_and_monitor_counts_over_the_window() -> None:
    start = snapshot(
        {"poultry-w1-001": (10, 10, 10, 0), "cattle-w1-001": (10, 10, 10, 0)},
        {
            "monitor-poultry-a": ("monitoring/v1/poultry/#", 10, 0),
            "monitor-all-b": ("monitoring/v1/#", 20, 0),
        },
    )
    end = snapshot(
        {"poultry-w1-001": (30, 28, 27, 2), "cattle-w1-001": (5, 5, 5, 0)},
        {
            "monitor-poultry-a": ("monitoring/v1/poultry/#", 30, 2),
            "monitor-all-b": ("monitoring/v1/#", 45, 0),
        },
    )
    totals = metrics.publisher_totals(start, end)
    assert totals["poultry"] == {"generated": 20, "published": 18, "acked": 17, "failed": 2}
    assert totals["cattle"]["published"] == 5  # the cattle worker restarted: counts from 0
    assert totals["all"]["published"] == 23
    delivery = metrics.monitor_delivery(start, end, totals)
    # poultry monitor: 20 received, 2 duplicates, expects 18; all-monitor: 25, expects 23
    assert delivery["monitor_expected"] == 18 + 23
    assert delivery["monitor_delivery"] == round((45 - 2) / 41, 5)


def test_broker_rates_survive_a_restart() -> None:
    samples = [
        {"t": 0.0, "messages_received": 100.0},
        {"t": 5.0, "messages_received": 150.0},
        {"t": 6.0, "messages_received": 10.0},
        {"t": 10.0, "messages_received": 50.0},
    ]
    rates = metrics.broker_rates(samples)
    assert rates["broker_messages_received_per_s"] == (50 + 10 + 40) / 10
    assert rates["broker_messages_sent_per_s"] is None


def test_resources_per_role() -> None:
    samples = [
        {"t": 1.0, "service": "device-poultry", "cpu_percent": 10.0, "mem_mib": 30.0},
        {"t": 1.0, "service": "device-cattle", "cpu_percent": 5.0, "mem_mib": 20.0},
        {"t": 3.0, "service": "device-poultry", "cpu_percent": 20.0, "mem_mib": 32.0},
        {"t": 1.0, "service": "broker", "cpu_percent": 1.0, "mem_mib": 5.0},
        {"t": 1.0, "service": "experiment-probe", "cpu_percent": 9.0, "mem_mib": 9.0},
    ]
    result = metrics.resource_metrics(samples)
    assert result["cpu_devices_avg"] == 17.5 and result["cpu_devices_max"] == 20.0
    assert result["mem_devices_max_mib"] == 50.0 and result["mem_broker_max_mib"] == 5.0
    assert not any(k.startswith("cpu_experiment") for k in result)


def test_recovery_after_a_failure() -> None:
    arrivals = {
        "a": [91.0, 95.0, 99.0, 112.0, 113.0],
        "b": [92.0, 96.0, 115.0],
        "late": [130.0],
    }  # not delivering before the failure: not counted
    outcome = metrics.recovery(arrivals, disrupted_at=100.0, restored_at=110.0)
    assert outcome == {"recovery_s": 5.0, "silence_max_s": 19.0, "devices_affected": 2}
    assert metrics.recovery({"a": [95.0]}, 100.0, 110.0)["recovery_s"] is None  # never back


def test_run_record_and_saturation() -> None:
    window = {
        "measurements": 99,
        "sequence_gaps": 1,
        "alerts_raised": {"warning": 2},
        "generated_to_received_ms": {"p50": 1.0, "p95": 2.0, "p99": 3.0, "max": 4.0, "avg": 1.0},
        "generated_to_stored_ms": {
            "p50": 500.0,
            "p95": 6000.0,
            "p99": 7000.0,
            "max": 8000.0,
            "avg": 600.0,
        },
    }
    result = {
        "run_id": "r",
        "since": "2026-11-16T00:00:00+00:00",
        "until": "2026-11-16T00:00:10+00:00",
        "start": snapshot({"poultry-w-001": (0, 0, 0, 0)}),
        "end": snapshot({"poultry-w-001": (100, 100, 90, 0)}),
        "window": window,
        "broker": [],
        "settled": True,
    }
    record = metrics.run_record(result, duration=10.0)
    assert record["published_per_s"] == 10.0 and record["acked_ratio"] == 0.9
    assert record["storage_delivery"] == 0.99 and record["alerts_raised"] == 2
    json.dumps(record)  # flat and serializable
    reasons = metrics.saturation_reasons([record], min_delivery=0.995, max_p95_ms=5000)
    assert len(reasons) == 2  # delivery below 0.995 and p95 above 5 s
    assert metrics.saturation_reasons([record], 0.9, 10_000) == []


# Report ------------------------------------------------------------------------------------


def test_report_aggregates_repetitions(tmp_path: Path) -> None:
    from monitoring_mqtt.experiments.report import build, t95
    from monitoring_mqtt.experiments.results import save_record, write_json

    s = scenario(
        name="qos",
        sweep={"mqtt.qos": [0, 1]},
        repetitions=2,
        report={"charts": ["delivery", "throughput", "duplicates", "nope"]},
    )
    write_json(
        tmp_path / "manifest.json",
        {"scenario": s.model_dump(mode="json", by_alias=True), "campaign": "c", "git": "abc"},
    )
    for qos in (0, 1):
        for rep, delivery in ((1, 0.98), (2, 1.0)):
            save_record(
                tmp_path,
                {
                    "scenario": "qos",
                    "point": qos + 1,
                    "label": f"q{qos}",
                    "repetition": rep,
                    "run_id": f"r{qos}{rep}",
                    "status": "ok",
                    "mqtt.qos": qos,
                    "storage_delivery": delivery,
                    "published_per_s": 10.0,
                    "monitor_duplicates": qos * rep,
                },
            )
    save_record(tmp_path, {"run_id": "broken", "status": "error", "error": "stack up failed"})
    report = build(tmp_path).read_text()
    summary = (tmp_path / "summary.csv").read_text().splitlines()
    assert len(summary) == 3  # header + 2 points
    assert "storage_delivery_ci95" in summary[0]
    assert (tmp_path / "charts" / "delivery.png").stat().st_size > 1000
    assert not (tmp_path / "charts" / "nope.png").exists()
    assert "| q1 |" in report and "0.99 ± " in report and "`broken`: error" in report
    assert round(t95(3), 3) == 4.303 and t95(100) == 1.96
    assert yaml.safe_load(yaml.safe_dump(s.model_dump(mode="json", by_alias=True)))
