"""Alerts: detected from the measurements, matched against the ground truth, stored."""

import time
from datetime import UTC, datetime

from harness import Observer, Stack

RUMEN_PH_HIGH = "rumen-ph-high"


def alerts(stack: Stack, **params) -> list[dict]:
    return stack.api("/api/alerts", limit=1000, **params)["items"]  # type: ignore[index]


def test_scheduled_anomaly_raises_and_clears_an_alert(stack: Stack, observer: Observer) -> None:
    device = observer.active("cattle")[0]

    def episodes():
        events = [e for e in observer.events(device) if e.kind == "anomaly" and e.source == "fault"]
        starts = [e for e in events if e.phase == "start"]
        ends = [e for e in events if e.phase == "end"]
        return list(zip(starts, ends, strict=False)) if ends else []

    (start, end), *_ = observer.wait_until(episodes, "a scheduled anomaly", 60)

    def matching_alert():
        for alert in alerts(stack, device_id=device, rule_id=RUMEN_PH_HIGH, state="cleared"):
            raised = datetime.fromisoformat(alert["raised_at"])
            if start.at <= raised <= end.at:
                return alert
        return None

    alert = observer.wait_until(matching_alert, f"{RUMEN_PH_HIGH} during the episode", 60)
    cleared = datetime.fromisoformat(alert["cleared_at"])
    assert cleared >= end.at  # cleared only after the anomaly ended
    assert alert["raised_value"] > alert["threshold"] >= alert["cleared_value"]
    detection = (datetime.fromisoformat(alert["raised_at"]) - start.at).total_seconds()
    assert detection < 5  # within a few publications (0.5 s each)


def test_live_anomaly_is_alerted_and_stored(stack: Stack, observer: Observer) -> None:
    device = observer.active("poultry")[0]
    t0 = datetime.now(UTC)
    assert stack.cli("device", "anomaly", device, "--for", "6s").returncode == 0
    stored = observer.wait_until(
        lambda: [a for a in alerts(stack, device_id=device, rule_id="ammonia-high")
                 if datetime.fromisoformat(a["raised_at"]) >= t0 and a["state"] == "cleared"],
        "ammonia-high raised and cleared", 60,
    )  # fmt: skip
    published = [a for a in observer.alerts if a.alert_id.hex == stored[0]["alert_id"].replace(
        "-", "")]  # fmt: skip
    assert {a.state.value for a in published} == {"raised", "cleared"}  # seen on the broker too


def test_no_alert_without_anomaly(stack: Stack) -> None:
    # Aquaculture devices never had an anomaly in this suite: noise alone never alerts.
    assert alerts(stack, domain="aquaculture") == []


def test_open_alert_survives_an_engine_restart(stack: Stack, observer: Observer) -> None:
    device = observer.active("poultry")[1]
    t0 = datetime.now(UTC)
    assert stack.cli("device", "anomaly", device).returncode == 0  # until resume
    observer.wait_until(
        lambda: [a for a in alerts(stack, device_id=device, state="active", rule_id="ammonia-high")
                 if datetime.fromisoformat(a["raised_at"]) >= t0],
        "ammonia-high open", 60,
    )  # fmt: skip
    before = observer.statuses["alerts"]
    assert stack.cli("stack", "restart", "alerts").returncode == 0
    observer.wait_until(
        lambda: (s := observer.statuses.get("alerts")) is not None and s.state == "online"
        and s.started_at > before.started_at and s.alerts is not None and s.alerts.open >= 1,  # type: ignore[union-attr]
        "alert engine restarted with the open alert restored",
    )  # fmt: skip
    time.sleep(2)  # anomalous values keep arriving: the restored alert must not be raised again
    assert stack.cli("device", "resume", device).returncode == 0
    observer.wait_until(
        lambda: all(a["state"] == "cleared" for a in alerts(
            stack, device_id=device, rule_id="ammonia-high", **{"from": t0.isoformat()})),
        "ammonia-high cleared", 60,
    )  # fmt: skip
    rows = alerts(stack, device_id=device, rule_id="ammonia-high", **{"from": t0.isoformat()})
    assert len(rows) == 1  # one alert for the whole episode, across the restart


def test_queries_reflect_the_run(stack: Stack, observer: Observer) -> None:
    devices = {d["device_id"] for d in stack.api("/api/devices")["items"]}  # type: ignore[index]
    assert set(observer.active()) <= devices
    run = stack.api("/api/runs/it-run")
    assert run["measurements"] > 0 and run["alerts"] >= 2  # type: ignore[index]
    assert {d["domain"] for d in run["domains"]} == {"aquaculture", "poultry", "cattle"}  # type: ignore[index]
    series = stack.api("/api/measurements/series", device_id=observer.active("cattle")[0],
                       bucket="10s")  # fmt: skip
    assert series["points"] and all(p["min"] <= p["avg"] <= p["max"] for p in series["points"])  # type: ignore[index]
    assert stack.api_status("/docs") == 200
