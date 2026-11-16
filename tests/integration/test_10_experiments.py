"""Experiments: a scenario runs end to end on the test stack and its report is written.

Runs last: every run of a scenario starts the stack again with its own experiment file.
"""

import csv
import shutil
from pathlib import Path

from harness import Stack

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = """\
schema_version: 1
name: it-check
description: Integration check of the experiment tooling
base: tests/integration/experiment.yaml
warmup: 3s
duration: 12s
repetitions: 1
sweep:
  mqtt.qos: [0, 1]
actions:
  - {at: 4s, do: restart, service: monitor-cattle}
report:
  x: mqtt.qos
  charts: [throughput, delivery, acked]
"""


def test_scenario_runs_and_reports(stack: Stack, tmp_path: Path) -> None:
    scenario = tmp_path / "it-check.yaml"
    scenario.write_text(SCENARIO)
    results = ROOT / "results" / "it-check"
    shutil.rmtree(results, ignore_errors=True)
    try:
        run = stack.cli("experiment", "run", str(scenario), timeout=900)
        assert run.returncode == 0, run.stdout + run.stderr
        (campaign,) = results.iterdir()
        rows = list(csv.DictReader((campaign / "runs.csv").open()))
        assert [r["mqtt.qos"] for r in rows] == ["0", "1"]
        for row in rows:
            assert row["status"] == "ok", row
            assert int(row["published"]) > 0 and float(row["storage_delivery"]) == 1.0
            assert int(row["stored"]) >= int(row["published"]) - 10  # window edges
            assert float(row["latency_mqtt_p95_ms"]) < 1000
            assert row["action1"] == "restart monitor-cattle"
            assert float(row["cpu_broker_avg"]) >= 0 and not row["containers_failed"]
        report = stack.cli("experiment", "report", str(campaign))
        assert report.returncode == 0, report.stdout + report.stderr
        assert (campaign / "charts" / "acked.png").is_file()
        assert "| mqtt.qos=1 |" in (campaign / "report.md").read_text()
    finally:
        shutil.rmtree(results, ignore_errors=True)
