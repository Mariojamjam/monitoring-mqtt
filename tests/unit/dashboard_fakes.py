"""A fake API for the dashboard tests: fixed answers, recorded calls, outages on demand."""

from datetime import UTC, datetime, timedelta

from monitoring_mqtt.dashboard.client import ApiUnavailableError

NOW = datetime.now(UTC).replace(microsecond=0)
RUN = "run-1"


def iso(at: datetime) -> str:
    return at.isoformat()


CATALOG = {
    "device_types": [
        {"name": "ammonia_detector", "domain": "poultry", "metric": "ammonia", "unit": "ppm",
         "bounds": {"minimum": 0, "maximum": 60}, "normal": {"minimum": 0, "maximum": 25},
         "anomaly_low": None, "anomaly_high": 40},
        {"name": "rumen_ph_bolus", "domain": "cattle", "metric": "rumen_ph", "unit": None,
         "bounds": {"minimum": 5, "maximum": 7.5}, "normal": {"minimum": 5.8, "maximum": 6.8},
         "anomaly_low": 5.2, "anomaly_high": 7.5},
    ],
    "alert_rules": [
        {"id": "ammonia-high", "device_type": "ammonia_detector", "severity": "warning",
         "condition": "above", "threshold": 25, "clear_at": 23, "after": 2, "clear_after": 2},
        {"id": "ammonia-critical", "device_type": "ammonia_detector", "severity": "critical",
         "condition": "above", "threshold": 35, "clear_at": 32, "after": 1, "clear_after": 1},
    ],
}  # fmt: skip


def device(device_id: str, domain: str, value: float, open_alerts: int = 0) -> dict:
    kind = {"poultry": ("ammonia_detector", "ammonia", "ppm"),
            "cattle": ("rumen_ph_bolus", "rumen_ph", None)}[domain]  # fmt: skip
    return {
        "device_id": device_id, "device_type": kind[0], "domain": domain, "metric": kind[1],
        "unit": kind[2], "site_id": "site-01", "worker_id": "abc123",
        "first_seen_at": iso(NOW - timedelta(hours=1)), "last_seen_at": iso(NOW),
        "last_value": value, "last_sequence": 100, "last_run_id": RUN, "open_alerts": open_alerts,
    }  # fmt: skip


def alert(rule: str, severity: str, *, minutes_ago: float, lasted: float | None) -> dict:
    raised = NOW - timedelta(minutes=minutes_ago)
    cleared = raised + timedelta(seconds=lasted) if lasted is not None else None
    return {
        "alert_id": f"00000000-0000-0000-0000-{abs(hash((rule, minutes_ago))) % 10**12:012d}",
        "rule_id": rule, "severity": severity,
        "state": "cleared" if cleared else "raised", "device_id": "poultry-abc123-001",
        "device_type": "ammonia_detector", "domain": "poultry", "metric": "ammonia",
        "unit": "ppm", "run_id": RUN, "condition": "above", "threshold": 25.0,
        "raised_value": 31.0, "raised_event_id": None, "raised_at": iso(raised),
        "cleared_value": 21.0 if cleared else None, "cleared_event_id": None,
        "cleared_at": iso(cleared) if cleared else None,
        "duration_seconds": lasted, "received_at": iso(raised), "stored_at": iso(raised),
    }  # fmt: skip


def measurement(sequence: int) -> dict:
    generated = NOW - timedelta(seconds=5 * (10 - sequence))
    return {
        "event_id": f"00000000-0000-0000-0000-{sequence:012d}", "run_id": RUN,
        "device_id": "poultry-abc123-001", "domain": "poultry", "metric": "ammonia",
        "site_id": "site-01", "value": 10.0 + sequence, "unit": "ppm", "sequence": sequence,
        "generated_at": iso(generated),
        "received_at": iso(generated + timedelta(milliseconds=8)),
        "stored_at": iso(generated + timedelta(milliseconds=1008)),
    }  # fmt: skip


def run_detail(run_id: str, measurements: int, gaps: int) -> dict:
    return {
        "run_id": run_id, "first_seen_at": iso(NOW - timedelta(hours=1)),
        "last_seen_at": iso(NOW), "measurements": measurements, "devices": 2, "alerts": 3,
        "domains": [{"domain": "poultry", "measurements": measurements, "devices": 1}],
        "duration_seconds": 3600.0, "measurements_per_second": measurements / 3600,
        "generated_to_received_ms": 8.0, "generated_to_stored_ms": 1010.0,
        "generated_to_stored_p95_ms": 1100.0, "generated_to_stored_max_ms": 1500.0,
        "sequence_gaps": gaps, "alerts_by_severity": {"warning": 2, "critical": 1},
    }  # fmt: skip


class FakeApi:
    def __init__(self) -> None:
        self.down = False
        self.calls: list[tuple[str, dict]] = []
        self.alert_list = [
            alert("ammonia-high", "warning", minutes_ago=10, lasted=60),
            alert("ammonia-high", "warning", minutes_ago=5, lasted=30),
            alert("ammonia-critical", "critical", minutes_ago=1, lasted=None),  # still open
        ]

    def _answer(self, name: str, filters: dict, value):
        self.calls.append((name, filters))
        if self.down:
            raise ApiUnavailableError("connection refused")
        return value

    def last(self, name: str) -> dict:
        return next(f for n, f in reversed(self.calls) if n == name)

    def stats(self, **f):
        return self._answer("stats", f, {
            "measurements": 1234, "devices": 2, "runs": 2, "alerts_total": 3, "alerts_open": 1,
            "last_stored_at": iso(NOW), "received_to_stored_ms": 1000.0,
            "generated_to_stored_ms": 1010.0,
        })  # fmt: skip

    def catalog(self, **f):
        return self._answer("catalog", f, CATALOG)

    def runs(self, **f):
        return self._answer("runs", f, [
            {k: v for k, v in run_detail(r, n, 0).items() if k in (
                "run_id", "first_seen_at", "last_seen_at", "measurements", "devices", "alerts")}
            for r, n in ((RUN, 1000), ("run-0", 234))
        ])  # fmt: skip

    def run(self, run_id, **f):
        details = {RUN: run_detail(RUN, 1000, 0), "run-0": run_detail("run-0", 234, 7)}
        return self._answer("run", {"run_id": run_id, **f}, details.get(run_id))

    def devices(self, **f):
        items = [device("poultry-abc123-001", "poultry", 31.0, open_alerts=1),
                 device("cattle-abc123-001", "cattle", 6.2)]  # fmt: skip
        if f.get("domain"):
            items = [d for d in items if d["domain"] == f["domain"]]
        return self._answer("devices", f, items)

    def device(self, device_id, **f):
        found = next((d for d in self.devices() if d["device_id"] == device_id), None)
        if found is not None:
            still_open = [a for a in self.alert_list if a["state"] == "raised"]
            found = {**found, "measurements": 10, "open_alerts": still_open}
        return self._answer("device", {"device_id": device_id, **f}, found)

    def measurements(self, **f):
        return self._answer("measurements", f, [measurement(s) for s in range(9, 0, -1)])

    def series(self, **f):
        points = [
            {"bucket_start": iso(NOW - timedelta(minutes=10 - i)), "count": 12,
             "avg": 10.0 + i, "min": 9.0 + i, "max": 11.0 + i}
            for i in range(10)
        ]  # fmt: skip
        return self._answer("series", f, {"device_id": f.get("device_id"),
                                          "domain": f.get("domain"), "bucket_seconds": 60,
                                          "points": points})  # fmt: skip

    def alerts(self, **f):
        items = self.alert_list
        if f.get("state") == "active":
            items = [a for a in items if a["state"] == "raised"]
        if f.get("severity"):
            items = [a for a in items if a["severity"] == f["severity"]]
        return self._answer("alerts", f, items)
