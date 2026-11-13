"""History: what the broker delivered is stored once, and survives outages of every hop."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from harness import Observer, Stack

from monitoring_mqtt.core.models import Measurement

SETTLE = 3.0  # flush interval (0.2 s) plus margin for a batch in flight


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


def stored_sequences(stack: Stack, device: str, since: float, until: float) -> list[int]:
    items = stack.all_measurements(device_id=device, **{"from": iso(since), "to": iso(until)})
    return sorted(i["sequence"] for i in items)


def assert_complete(stack: Stack, observer: Observer, since: float, until: float) -> None:
    """Every device: what it published in [since, until) is stored, without gaps."""
    for device in observer.active():
        observer.wait_until(
            lambda d=device: (s := stored_sequences(stack, d, since, until))
            and s == list(range(s[0], s[-1] + 1)) and len(s) >= (until - since) / 0.5 - 2,
            lambda d=device: f"{d} stored without gaps: {stored_sequences(stack, d, since, until)}",
        )  # fmt: skip


def test_schema_matches_the_migrations(stack: Stack) -> None:
    check = (
        "from alembic.autogenerate import compare_metadata\n"
        "from alembic.migration import MigrationContext\n"
        "from monitoring_mqtt.storage.database import make_engine\n"
        "from monitoring_mqtt.storage.schema import metadata\n"
        "from monitoring_mqtt.storage.settings import DatabaseSettings\n"
        "with make_engine(DatabaseSettings.from_env()).connect() as c:\n"
        "    print(compare_metadata(MigrationContext.configure(c), metadata))\n"
    )
    result = stack.compose("exec", "-T", "api", "python", "-c", check)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_everything_the_observer_saw_is_stored(stack: Stack, observer: Observer) -> None:
    cutoff = time.time() - SETTLE
    for device in observer.active():
        seen = {s.event_id for s in observer.samples(device) if s.received_at < cutoff}
        stored = {i["event_id"] for i in stack.all_measurements(device_id=device)}
        assert seen <= stored, f"{device}: {len(seen - stored)} not stored"


def test_a_repeated_event_is_stored_once(stack: Stack, observer: Observer) -> None:
    device = observer.active("poultry")[0]
    (row,) = stack.api("/api/measurements", device_id=device, limit=1)["items"]  # type: ignore[index]
    info = stack.api(f"/api/devices/{device}")
    m = Measurement.model_validate({
        **{k: row[k] for k in ("event_id", "run_id", "device_id", "domain", "site_id", "metric",
                               "value", "unit", "sequence")},
        "device_type": info["device_type"], "timestamp": row["generated_at"],  # type: ignore[index]
    })  # fmt: skip
    for _ in range(3):
        observer.publish(m.topic, m.to_payload())
    time.sleep(SETTLE)
    at = datetime.fromisoformat(row["generated_at"])
    same = stack.api("/api/measurements", device_id=device, **{
        "from": at.isoformat(), "to": (at + timedelta(microseconds=1)).isoformat()})  # fmt: skip
    assert [i["event_id"] for i in same["items"]] == [row["event_id"]]  # type: ignore[index]


def test_three_times_are_kept_in_order(stack: Stack, observer: Observer) -> None:
    rows = stack.api("/api/measurements", limit=200)["items"]  # type: ignore[index]
    for row in rows:
        generated, received, stored = (datetime.fromisoformat(row[k]) for k in
                                       ("generated_at", "received_at", "stored_at"))  # fmt: skip
        assert generated <= received <= stored, row
    stats = stack.api("/api/stats")
    assert stats["measurements"] > 0 and stats["generated_to_stored_ms"] < 5000  # type: ignore[index]


@pytest.mark.parametrize("service", ["api", "postgres", "ingestion"])
def test_history_survives_an_outage(stack: Stack, observer: Observer, service: str) -> None:
    container = stack.container(service)
    since = time.time()
    assert stack.docker("stop", container).returncode == 0
    try:
        if service == "postgres":
            observer.wait_until(lambda: stack.api_status("/ready") == 503, "API reports 503", 15)
        time.sleep(5)
    finally:
        stack.docker("start", container)
    observer.wait_until(lambda: stack.api_status("/ready") == 200, "API ready again", 60)
    until = time.time()
    time.sleep(SETTLE)
    # Kept by the ingestion buffer (API or database down) or by the broker (ingestion down):
    # nothing published during the outage is missing.
    assert_complete(stack, observer, since, until)
