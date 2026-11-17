"""Where a campaign's results go (``results/<scenario>/<campaign>/``) and how to read them.

```text
results/qos/20261116T140000Z/
├── manifest.json          scenario, git commit, machine, versions, start and end
├── scenario.yaml          the scenario file as it was run
├── runs.csv, runs.jsonl   one row per run: parameters and metrics
└── runs/<run_id>/
    ├── experiment.yaml    the experiment file `stack up` applied
    ├── stack-up.log       its output
    ├── probe.log          the probe's log (stderr)
    ├── result.json        what the probe measured
    └── resources.csv      CPU and memory per container, every few seconds
```
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

RESULTS_DIR = Path("results")
RUNS_JSONL = "runs.jsonl"
RUNS_CSV = "runs.csv"
MANIFEST = "manifest.json"
# Leading columns of runs.csv; metrics follow in the order they first appear.
LEADING = ["scenario", "point", "label", "repetition", "run_id", "status"]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    rows = list(rows)
    columns = [c for c in LEADING if any(c in r for r in rows)]
    for row in rows:
        columns += [k for k in row if k not in columns]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _cell(row.get(k)) for k in columns})


def _cell(value: Any) -> Any:
    if isinstance(value, dict | list):
        return json.dumps(value, default=str)
    return "" if value is None else value


def load_records(campaign: Path) -> list[dict[str, Any]]:
    path = campaign / RUNS_JSONL
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def save_record(campaign: Path, record: Mapping[str, Any]) -> None:
    """Append one run and rewrite runs.csv (a campaign stays readable if interrupted)."""
    records = [r for r in load_records(campaign) if r.get("run_id") != record.get("run_id")]
    records.append(dict(record))
    (campaign / RUNS_JSONL).write_text(
        "".join(json.dumps(r, default=str) + "\n" for r in records), encoding="utf-8"
    )
    write_csv(campaign / RUNS_CSV, records)
