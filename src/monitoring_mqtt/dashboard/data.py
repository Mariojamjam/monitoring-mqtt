"""Pure transformations: API responses → tables ready for charts (pandas, no Streamlit)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

# Time windows offered by the dashboard (None = the whole selected run).
WINDOWS: dict[str, int | None] = {
    "15 min": 15 * 60,
    "1 h": 3600,
    "6 h": 6 * 3600,
    "24 h": 24 * 3600,
    "Whole run": None,
}
BUCKETS = [5, 10, 30, 60, 300, 900, 1800, 3600, 6 * 3600, 24 * 3600]
TARGET_POINTS = 180


@dataclass(frozen=True)
class Window:
    """The period a page shows: from ``since`` (None = the beginning) to now."""

    since: datetime | None
    seconds: float  # its length, for the bucket size

    @property
    def bucket_seconds(self) -> int:
        return pick_bucket(self.seconds)

    @property
    def bucket(self) -> str:
        return bucket_label(self.bucket_seconds)


def pick_bucket(window_seconds: float, points: int = TARGET_POINTS) -> int:
    """The smallest bucket that keeps a chart at about ``points`` points."""
    wanted = window_seconds / points
    return next((b for b in BUCKETS if b >= wanted), BUCKETS[-1])


def bucket_label(seconds: int) -> str:
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def floor_time(at: datetime, step_seconds: float) -> datetime:
    """``at`` rounded down to a multiple of the step: equal keys within one refresh, so
    the cache answers repeated reruns (and several browsers) with one API call."""
    step = max(1, int(step_seconds))
    return datetime.fromtimestamp(math.floor(at.timestamp() / step) * step, UTC)


def make_window(
    window_seconds: int | None,
    *,
    now: datetime,
    run: dict[str, Any] | None = None,
    step_seconds: float = 5,
) -> Window:
    if window_seconds is not None:
        return Window(
            floor_time(now - timedelta(seconds=window_seconds), step_seconds), window_seconds
        )
    if run is None:  # every run: as far back as a day of buckets allows
        return Window(None, 24 * 3600)
    start = parse_time(run["first_seen_at"])
    return Window(start, max(60.0, (now - start).total_seconds()))


def parse_time(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


# Catalog ----------------------------------------------------------------------------------


def device_type_info(catalog: dict[str, Any], device_type: str) -> dict[str, Any] | None:
    return next((t for t in catalog["device_types"] if t["name"] == device_type), None)


def domain_type(catalog: dict[str, Any], domain: str) -> dict[str, Any] | None:
    """The (first) device type of a domain: its normal band and unit for domain charts."""
    return next((t for t in catalog["device_types"] if t["domain"] == domain), None)


def domains(catalog: dict[str, Any]) -> list[str]:
    return sorted({t["domain"] for t in catalog["device_types"]})


def rules_for(catalog: dict[str, Any], device_type: str) -> list[dict[str, Any]]:
    return [r for r in catalog["alert_rules"] if r["device_type"] == device_type]


def value_state(value: float, normal: dict[str, float] | None) -> str:
    if normal is None:
        return "—"
    if value < normal["minimum"]:
        return "low"
    if value > normal["maximum"]:
        return "high"
    return "normal"


# Frames -----------------------------------------------------------------------------------


def series_frame(series: dict[str, Any]) -> pd.DataFrame:
    rows = [
        {
            "time": parse_time(p["bucket_start"]),
            "avg": p["avg"],
            "min": p["min"],
            "max": p["max"],
            "count": p["count"],
        }
        for p in series["points"]
    ]
    return pd.DataFrame(rows, columns=["time", "avg", "min", "max", "count"])


def devices_frame(
    devices: list[dict[str, Any]], catalog: dict[str, Any], now: datetime
) -> pd.DataFrame:
    rows = []
    for d in devices:
        info = device_type_info(catalog, d["device_type"])
        last = parse_time(d["last_seen_at"])
        rows.append(
            {
                "device": d["device_id"],
                "domain": d["domain"],
                "value": d["last_value"],
                "unit": d["unit"] or "",
                "state": value_state(d["last_value"], info["normal"] if info else None),
                "seen_s_ago": round((now - last).total_seconds(), 1) if last else None,
                "open_alerts": d["open_alerts"],
                "worker": d["worker_id"],
                "site": d["site_id"],
                "run": d["last_run_id"],
            }
        )
    columns = [
        "device",
        "domain",
        "value",
        "unit",
        "state",
        "seen_s_ago",
        "open_alerts",
        "worker",
        "site",
        "run",
    ]
    return pd.DataFrame(rows, columns=columns)


ALERT_COLUMNS = [
    "alert_id",
    "rule",
    "severity",
    "state",
    "device",
    "domain",
    "condition",
    "threshold",
    "raised_value",
    "cleared_value",
    "start",
    "end",
    "open",
    "duration_s",
]


def alerts_frame(alerts: list[dict[str, Any]], now: datetime) -> pd.DataFrame:
    """One row per alert, as an interval: an open alert lasts until ``now``."""
    rows = []
    for a in alerts:
        raised, cleared = parse_time(a["raised_at"]), parse_time(a["cleared_at"])
        start = raised or cleared
        is_open = a["state"] == "raised"
        end = now if is_open else (cleared or start)
        rows.append(
            {
                "alert_id": a["alert_id"],
                "rule": a["rule_id"],
                "severity": a["severity"],
                "state": "open" if is_open else "cleared",
                "device": a["device_id"],
                "domain": a["domain"],
                "condition": a["condition"],
                "threshold": a["threshold"],
                "raised_value": a["raised_value"],
                "cleared_value": a["cleared_value"],
                "start": start,
                "end": end,
                "open": is_open,
                "duration_s": round((end - start).total_seconds(), 1) if start and end else None,
            }
        )
    return pd.DataFrame(rows, columns=ALERT_COLUMNS)


def rule_summary(alerts: pd.DataFrame) -> pd.DataFrame:
    """Per rule: how many alerts, how many still open, how long they lasted."""
    columns = ["rule", "severity", "alerts", "open", "mean_duration_s", "max_duration_s"]
    if alerts.empty:
        return pd.DataFrame(columns=columns)
    cleared = alerts[~alerts["open"]]
    summary = alerts.groupby(["rule", "severity"]).agg(
        alerts=("alert_id", "count"), open=("open", "sum")
    )
    durations = cleared.groupby(["rule", "severity"])["duration_s"].agg(["mean", "max"])
    summary = summary.join(durations).rename(
        columns={"mean": "mean_duration_s", "max": "max_duration_s"}
    )
    summary["open"] = summary["open"].astype(int)
    summary = summary.reset_index().sort_values(["alerts", "rule"], ascending=[False, True])
    return summary.round(1)[columns].reset_index(drop=True)


def measurements_frame(items: list[dict[str, Any]]) -> pd.DataFrame:
    """Stored measurements with their three times and the delays between them."""
    rows = []
    for m in items:
        generated, received, stored = (
            parse_time(m[k]) for k in ("generated_at", "received_at", "stored_at")
        )
        rows.append(
            {
                "generated_at": generated,
                "device": m["device_id"],
                "value": m["value"],
                "sequence": m["sequence"],
                "mqtt_ms": round((received - generated).total_seconds() * 1000, 1),
                "storage_ms": round((stored - received).total_seconds() * 1000, 1),
                "received_at": received,
                "stored_at": stored,
                "run": m["run_id"],
                "event_id": m["event_id"],
            }
        )
    columns = [
        "generated_at",
        "device",
        "value",
        "sequence",
        "mqtt_ms",
        "storage_ms",
        "received_at",
        "stored_at",
        "run",
        "event_id",
    ]
    return pd.DataFrame(rows, columns=columns)


# Runs -------------------------------------------------------------------------------------

RUN_METRICS: list[tuple[str, str]] = [
    ("duration_seconds", "Duration (s)"),
    ("devices", "Devices"),
    ("measurements", "Measurements"),
    ("measurements_per_second", "Measurements per second"),
    ("sequence_gaps", "Missing measurements (sequence gaps)"),
    ("generated_to_received_ms", "Device → ingestion, average (ms)"),
    ("generated_to_stored_ms", "Device → database, average (ms)"),
    ("generated_to_stored_p95_ms", "Device → database, p95 (ms)"),
    ("generated_to_stored_max_ms", "Device → database, max (ms)"),
    ("alerts", "Alerts"),
]


def run_metrics(run: dict[str, Any]) -> dict[str, Any]:
    """The comparable numbers of one run, by label (alerts and domains flattened)."""
    values = {label: run.get(key) for key, label in RUN_METRICS}
    for severity, count in sorted(run.get("alerts_by_severity", {}).items()):
        values[f"Alerts: {severity}"] = count
    for domain in run.get("domains", []):
        values[f"Measurements: {domain['domain']}"] = domain["measurements"]
    return values


def compare_runs(a: dict[str, Any], b: dict[str, Any]) -> pd.DataFrame:
    """Side by side: one row per metric, columns A and B, and B - A."""
    first, second = run_metrics(a), run_metrics(b)
    labels = list(dict.fromkeys([*first, *second]))
    rows = []
    for label in labels:
        x, y = first.get(label), second.get(label)
        diff = (
            round(y - x, 3) if isinstance(x, int | float) and isinstance(y, int | float) else None
        )
        rows.append({"metric": label, "A": x, "B": y, "difference": diff})
    return pd.DataFrame(rows)
