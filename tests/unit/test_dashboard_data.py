from datetime import UTC, datetime, timedelta

import pandas as pd
from dashboard_fakes import CATALOG, NOW, FakeApi, device, measurement, run_detail

from monitoring_mqtt.dashboard import charts, data


def test_bucket_keeps_charts_readable() -> None:
    assert data.pick_bucket(15 * 60) == 5  # 180 points
    assert data.pick_bucket(3600) == 30
    assert data.pick_bucket(24 * 3600) == 900
    assert data.pick_bucket(10**9) == 24 * 3600
    assert [data.bucket_label(s) for s in (5, 60, 300, 3600, 90)] == ["5s", "1m", "5m", "1h",
                                                                        "90s"]  # fmt: skip


def test_window_start_is_stable_within_a_refresh() -> None:
    a = datetime(2026, 11, 14, 12, 0, 1, tzinfo=UTC)
    b = a + timedelta(seconds=3)
    wa, wb = (data.make_window(900, now=t, step_seconds=5) for t in (a, b))
    assert wa.since == wb.since == datetime(2026, 11, 14, 11, 45, tzinfo=UTC)  # one cache key
    assert wa.bucket == "5s"
    run = {"first_seen_at": "2026-11-14T10:00:00+00:00"}
    whole = data.make_window(None, now=a, run=run)
    assert whole.since == datetime(2026, 11, 14, 10, tzinfo=UTC) and whole.seconds == 7201
    assert data.make_window(None, now=a).since is None  # every run


def test_device_state_against_the_normal_band() -> None:
    frame = data.devices_frame([device("p", "poultry", 31.0), device("c", "cattle", 5.0)],
                               CATALOG, NOW + timedelta(seconds=7))  # fmt: skip
    assert frame["state"].tolist() == ["high", "low"]
    assert frame["seen_s_ago"].tolist() == [7.0, 7.0]
    assert data.value_state(1.0, None) == "—"


def test_alerts_become_intervals_and_rule_summaries() -> None:
    frame = data.alerts_frame(FakeApi().alert_list, NOW)
    assert frame["state"].tolist() == ["cleared", "cleared", "open"]
    open_alert = frame[frame["open"]].iloc[0]
    assert open_alert["end"] == NOW and open_alert["duration_s"] == 60.0  # open: lasts until now
    summary = data.rule_summary(frame)
    high = summary[summary["rule"] == "ammonia-high"].iloc[0]
    assert (high["alerts"], high["open"], high["mean_duration_s"]) == (2, 0, 45.0)
    critical = summary[summary["rule"] == "ammonia-critical"].iloc[0]
    assert critical["open"] == 1 and pd.isna(critical["mean_duration_s"])  # none cleared yet
    assert data.rule_summary(data.alerts_frame([], NOW)).empty


def test_measurement_delays() -> None:
    frame = data.measurements_frame([measurement(1)])
    assert (frame["mqtt_ms"].item(), frame["storage_ms"].item()) == (8.0, 1000.0)


def test_runs_side_by_side() -> None:
    table = data.compare_runs(run_detail("a", 100, 0), run_detail("b", 150, 4))
    rows = table.set_index("metric")
    assert rows.loc["Measurements", "difference"] == 50
    assert rows.loc["Missing measurements (sequence gaps)", "B"] == 4
    assert rows.loc["Alerts: critical", "A"] == 1
    assert "Measurements: poultry" in rows.index


def test_charts_build() -> None:
    series = data.series_frame(FakeApi().series(device_id="p"))
    alerts = data.alerts_frame(FakeApi().alert_list, NOW)
    chart = charts.series_chart(series, unit="ppm", normal={"minimum": 0, "maximum": 25},
                                rules=CATALOG["alert_rules"], alerts=alerts)  # fmt: skip
    spec = chart.to_dict()
    # normal band, alerts, thresholds and their labels, min-max, average, hover rule and point
    assert len(spec["layer"]) == 8
    assert charts.alerts_timeline(alerts).to_dict()["mark"]["type"] == "bar"
    assert data.series_frame({"points": []}).empty
