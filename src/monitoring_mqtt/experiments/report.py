"""Campaign report: ``python -m monitoring_mqtt.experiments.report <results folder>``.

Aggregates the repetitions of every sweep point (mean, standard deviation, 95% confidence
interval with Student's t, minimum and maximum) and writes, into the campaign folder:

- ``summary.csv``: one row per point;
- ``charts/<name>.png``: one chart per entry of the scenario's ``report.charts``, the mean
  per point with its confidence interval as error bars;
- ``report.md``: scenario, method, environment, the summary table and the charts.

Needs the ``experiments`` dependency group (pandas, matplotlib); `uv sync` installs it.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # files only, no display

import matplotlib.pyplot as plt
import pandas as pd

from monitoring_mqtt.experiments.results import MANIFEST, load_records, read_json
from monitoring_mqtt.experiments.scenario import Scenario

# Two-sided 95% Student's t for n - 1 degrees of freedom (n repetitions); 1.96 beyond 30.
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
       9: 2.262, 10: 2.228, 15: 2.131, 20: 2.086, 25: 2.060, 30: 2.042}  # fmt: skip
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
# Line styles as a second encoding: series that coincide stay visible, and identity never
# depends on colour alone.
LINE_STYLES = ["-", "--", ":", "-."]
MARKERS = ["o", "s", "^", "D"]
TEXT = "#52514e"
GRID = "#e4e3df"


@dataclass(frozen=True)
class Series:
    column: str
    label: str


@dataclass(frozen=True)
class Chart:
    title: str
    unit: str
    series: tuple[Series, ...]
    from_zero: bool = True  # False for ratios near 1, where the differences matter


CHARTS: dict[str, Chart] = {
    "throughput": Chart("Throughput", "messages per second", (
        Series("published_per_s", "published by the devices"),
        Series("stored_per_s", "stored in the database"),
    )),
    "latency": Chart("Latency, device to ingestion consumer (MQTT path)", "ms", (
        Series("latency_mqtt_p50_ms", "p50"),
        Series("latency_mqtt_p95_ms", "p95"),
        Series("latency_mqtt_p99_ms", "p99"),
    )),
    "latency_storage": Chart("Latency, device to database", "ms", (
        Series("latency_storage_p50_ms", "p50"),
        Series("latency_storage_p95_ms", "p95"),
        Series("latency_storage_p99_ms", "p99"),
    )),
    "delivery": Chart("Delivery", "fraction delivered", (
        Series("storage_delivery", "stored / generated in the window"),
        Series("monitor_delivery", "received by the monitors / expected"),
    ), from_zero=False),
    "acked": Chart("Publications confirmed", "acked / published", (
        Series("acked_ratio", "acked / published"),
    ), from_zero=False),
    "duplicates": Chart("Duplicate deliveries", "messages", (
        Series("monitor_duplicates", "at the monitors"),
        Series("ingestion_duplicates", "at the ingestion consumer"),
    )),
    "gaps": Chart("Measurements lost (sequence gaps)", "measurements", (
        Series("sequence_gaps", "missing in the database"),
    )),
    "cpu": Chart("CPU, average", "% of one core", (
        Series("cpu_broker_avg", "broker"),
        Series("cpu_devices_avg", "device workers"),
        Series("cpu_monitors_avg", "monitors"),
        Series("cpu_ingestion_avg", "ingestion"),
    )),
    "memory": Chart("Memory, peak", "MiB", (
        Series("mem_broker_max_mib", "broker"),
        Series("mem_devices_max_mib", "device workers"),
        Series("mem_monitors_max_mib", "monitors"),
    )),
    "memory_per_device": Chart("Memory of the device workers per device", "MiB per device", (
        Series("mem_devices_per_device_mib", "device workers / devices"),
    )),
    "broker": Chart("Broker traffic", "messages per second", (
        Series("broker_messages_received_per_s", "received"),
        Series("broker_messages_sent_per_s", "sent"),
    )),
    "recovery": Chart("Recovery after the failure", "seconds", (
        Series("action1_recovery_s", "until every device delivers again"),
        Series("action1_silence_max_s", "longest silence of a device"),
    )),
}  # fmt: skip

# Columns of the summary table in report.md (when present), with short headers.
KEY_METRICS = [
    ("devices", "devices"),
    ("published_per_s", "published/s"),
    ("storage_delivery", "storage delivery"),
    ("monitor_delivery", "monitor delivery"),
    ("monitor_duplicates", "duplicates"),
    ("sequence_gaps", "gaps"),
    ("latency_mqtt_p95_ms", "MQTT p95 ms"),
    ("latency_storage_p95_ms", "storage p95 ms"),
    ("cpu_broker_avg", "broker CPU %"),
    ("action1_recovery_s", "recovery s"),
]


def t95(n: int) -> float:
    if n < 2:
        return float("nan")
    df = n - 1
    known = [k for k in sorted(T95) if k <= df]
    return T95[known[-1]] if df <= 30 else 1.96


def derived(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    if "mem_devices_avg_mib" in frame and "devices" in frame:
        frame["mem_devices_per_device_mib"] = frame["mem_devices_avg_mib"] / frame["devices"]
    return frame


def summarize(frame: pd.DataFrame, x: str | None) -> pd.DataFrame:
    """One row per sweep point: mean, std, ci95, min, max of every numeric metric."""
    keys = ["point", "label"] + ([x] if x and x in frame and x not in ("point", "label") else [])
    numeric = [c for c in frame.columns if c not in keys and c != "repetition"
               and pd.api.types.is_numeric_dtype(frame[c])]  # fmt: skip
    rows = []
    for values, group in frame.groupby(keys, sort=True, dropna=False):
        row = dict(zip(keys, values if isinstance(values, tuple) else (values,), strict=True))
        row["repetitions"] = len(group)
        for column in numeric:
            data = group[column].dropna()
            if data.empty:
                continue
            n = len(data)
            std = float(data.std(ddof=1)) if n > 1 else float("nan")
            row[f"{column}_mean"] = float(data.mean())
            row[f"{column}_std"] = std
            row[f"{column}_ci95"] = t95(n) * std / math.sqrt(n) if n > 1 else float("nan")
            row[f"{column}_min"] = float(data.min())
            row[f"{column}_max"] = float(data.max())
        rows.append(row)
    return pd.DataFrame(rows).sort_values("point").reset_index(drop=True)


def x_values(summary: pd.DataFrame, x: str | None) -> tuple[list[Any], str, bool]:
    """Values for the x axis, its label, and whether it is numeric (else categorical)."""
    if x and x in summary:
        values = list(summary[x])
        numeric = all(isinstance(v, int | float) and not isinstance(v, bool) for v in values)
        return values, x, numeric
    return list(summary["label"]), "sweep point", False


def plot(chart: Chart, summary: pd.DataFrame, x: str | None, path: Path) -> bool:
    series = [s for s in chart.series if f"{s.column}_mean" in summary]
    if not series:
        return False
    xs, x_label, numeric = x_values(summary, x)
    positions = xs if numeric else list(range(len(xs)))
    fig, ax = plt.subplots(figsize=(7.2, 4.0), dpi=150)
    for i, s in enumerate(series):
        mean = summary[f"{s.column}_mean"]
        ci = summary.get(f"{s.column}_ci95")
        ax.errorbar(
            positions,
            mean,
            yerr=None if ci is None or ci.isna().all() else ci,
            label=s.label,
            color=SERIES_COLORS[i % len(SERIES_COLORS)],
            linestyle=LINE_STYLES[i % len(LINE_STYLES)],
            marker=MARKERS[i % len(MARKERS)],
            markersize=5,
            linewidth=2,
            capsize=4,
        )  # fmt: skip
    if not numeric:
        ax.set_xticks(positions, [str(v) for v in xs], rotation=0)
    ax.set_title(chart.title, loc="left", fontsize=11, color="#0b0b0b")
    ax.set_xlabel(x_label, color=TEXT)
    ax.set_ylabel(chart.unit, color=TEXT)
    ax.tick_params(colors=TEXT, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    if chart.from_zero:
        ax.set_ylim(bottom=0)
    if len(series) > 1:
        ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    return True


def _fmt(value: Any, ci: Any = None) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    text = f"{value:.4g}" if isinstance(value, float) else str(value)
    if ci is not None and isinstance(ci, float) and not math.isnan(ci):
        text += f" ± {ci:.2g}"
    return text


def markdown(scenario: Scenario, manifest: dict[str, Any], records: list[dict[str, Any]],
             summary: pd.DataFrame, charts: list[tuple[str, Chart]]) -> str:  # fmt: skip
    failed = [r for r in records if r.get("status") not in ("ok", "unsettled")]
    lines = [
        f"# Experiment: {scenario.name}",
        "",
        scenario.description or "",
        "",
        "## Method",
        "",
        f"Every sweep point ran {scenario.repetitions} time(s). Each run started the stack "
        f"with its own experiment file (a new run id), waited until every device reported, "
        f"warmed up for {scenario.warmup:g} s and measured a window of {scenario.duration:g} s. "
        "Counts and rates cover the window only. Values are means over the repetitions; "
        "± is the 95% confidence interval (Student's t). Metric definitions: "
        "docs/evaluation/experiments.md.",
        "",
        "| | |",
        "|---|---|",
        f"| Base experiment | `{scenario.base}` |",
        f"| Fixed overrides | `{scenario.set}` |" if scenario.set else "| Fixed overrides | none |",
        f"| Sweep | `{scenario.sweep}` |" if scenario.sweep else "| Sweep | none |",
        f"| Actions | {', '.join(a.describe() + f' at {a.at:g}s' for a in scenario.actions)} |"
        if scenario.actions else "| Actions | none |",
        f"| Campaign | `{manifest.get('campaign')}`, {manifest.get('started_at')} → "
        f"{manifest.get('finished_at', 'unfinished')} |",
        f"| Code | `{manifest.get('git')}` (v{manifest.get('version')}) |",
        f"| Machine | {manifest.get('machine', {}).get('node')} "
        f"({manifest.get('machine', {}).get('roles')}); broker "
        f"{manifest.get('machine', {}).get('broker')} |",
        "",
        "## Results",
        "",
    ]  # fmt: skip
    present = [(c, h) for c, h in KEY_METRICS if f"{c}_mean" in summary]
    lines.append("| Point | " + " | ".join(h for _, h in present) + " |")
    lines.append("|---|" + "---|" * len(present))
    for _, row in summary.iterrows():
        cells = [_fmt(row.get(f"{c}_mean"), row.get(f"{c}_ci95")) for c, _ in present]
        lines.append(f"| {row['label']} | " + " | ".join(cells) + " |")
    lines.append("")
    if manifest.get("saturated"):
        lines += [f"**Saturated:** {'; '.join(manifest['saturated'])}. Larger points were "
                  "skipped.", ""]  # fmt: skip
    if failed:
        lines += ["**Runs not measured:**", ""]
        lines += [f"- `{r.get('run_id')}`: {r.get('status')}, {r.get('error')}" for r in failed]
        lines.append("")
    for name, chart in charts:
        lines += [f"### {chart.title}", "", f"![{chart.title}](charts/{name}.png)", ""]
    lines += ["Raw data: `runs.csv` (one row per run), `summary.csv` (one row per point), "
              "`runs/<run_id>/` (probe measurements and resource samples).", ""]  # fmt: skip
    return "\n".join(lines)


def build(campaign: Path) -> Path:
    manifest = read_json(campaign / MANIFEST)
    scenario = Scenario.model_validate(manifest["scenario"])
    records = load_records(campaign)
    measured = [r for r in records if r.get("status") in ("ok", "unsettled")]
    if not measured:
        raise SystemExit(f"{campaign}: no measured run yet")
    frame = derived(pd.DataFrame(measured))
    x = scenario.x_key
    summary = summarize(frame, x)
    summary.to_csv(campaign / "summary.csv", index=False)
    drawn = []
    for name in scenario.report.charts:
        chart = CHARTS.get(name)
        if chart is None:
            print(f"unknown chart {name!r}; known: {', '.join(CHARTS)}", file=sys.stderr)
            continue
        if plot(chart, summary, x, campaign / "charts" / f"{name}.png"):
            drawn.append((name, chart))
    report = campaign / "report.md"
    report.write_text(markdown(scenario, manifest, records, summary, drawn), encoding="utf-8")
    return report


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m monitoring_mqtt.experiments.report <results folder>",
              file=sys.stderr)  # fmt: skip
        return 2
    report = build(Path(sys.argv[1]))
    print(f"Report: {report}")
    print(f"Charts: {report.parent / 'charts'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
