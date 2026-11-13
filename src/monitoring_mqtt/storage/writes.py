"""Idempotent writes: every event is stored once, however many times it arrives.

QoS 1 delivers at least once, and the ingestion consumer retries a batch whose answer it
did not get, so the same event can reach the API several times. Measurements are unique by
``event_id`` (``INSERT … ON CONFLICT DO NOTHING``); an alert is one row per ``alert_id``,
opened by ``raised`` and closed by ``cleared`` in whichever order they arrive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, case, func
from sqlalchemy.dialects.postgresql import insert

from monitoring_mqtt.core.alerts import AlertEvent, AlertState
from monitoring_mqtt.core.identity import worker_of
from monitoring_mqtt.core.models import Measurement
from monitoring_mqtt.storage.schema import alerts, devices, measurements, runs


@dataclass(frozen=True)
class Received[T]:
    event: T
    received_at: datetime


@dataclass(frozen=True)
class WriteResult:
    stored: int
    duplicates: int


def store_measurements(conn: Connection, items: list[Received[Measurement]]) -> WriteResult:
    """Store a batch in one transaction (the caller commits)."""
    if not items:
        return WriteResult(0, 0)
    _upsert_runs(conn, items)
    _upsert_devices(conn, items)
    rows = [
        {
            "event_id": m.event_id, "run_id": m.run_id, "device_id": m.device_id,
            "domain": m.domain, "metric": m.metric, "site_id": m.site_id, "value": m.value,
            "unit": m.unit, "sequence": m.sequence, "generated_at": m.timestamp,
            "received_at": item.received_at,
        }
        for item in items
        for m in (item.event,)
    ]  # fmt: skip
    statement = (
        insert(measurements)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(measurements.c.event_id)
    )
    stored = len(conn.execute(statement).all())
    return WriteResult(stored=stored, duplicates=len(items) - stored)


def _upsert_runs(conn: Connection, items: list[Received[Measurement]]) -> None:
    seen: dict[str, tuple[datetime, datetime]] = {}
    for item in items:
        at = item.event.timestamp
        first, last = seen.get(item.event.run_id, (at, at))
        seen[item.event.run_id] = (min(first, at), max(last, at))
    statement = insert(runs).values(
        [{"run_id": r, "first_seen_at": f, "last_seen_at": last} for r, (f, last) in seen.items()]
    )
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=["run_id"],
            set_={
                "first_seen_at": func.least(runs.c.first_seen_at, statement.excluded.first_seen_at),
                "last_seen_at": func.greatest(runs.c.last_seen_at, statement.excluded.last_seen_at),
            },
        )
    )


def _upsert_devices(conn: Connection, items: list[Received[Measurement]]) -> None:
    # One row per device (Postgres cannot update the same row twice in one statement):
    # its latest measurement, and the earliest time it was seen in this batch.
    latest: dict[str, Measurement] = {}
    first: dict[str, datetime] = {}
    for item in items:
        m = item.event
        if m.device_id not in latest or m.timestamp > latest[m.device_id].timestamp:
            latest[m.device_id] = m
        first[m.device_id] = min(first.get(m.device_id, m.timestamp), m.timestamp)
    rows = [
        {
            "device_id": m.device_id, "device_type": m.device_type, "domain": m.domain,
            "metric": m.metric, "unit": m.unit, "site_id": m.site_id,
            "worker_id": worker_of(m.device_id, m.domain), "first_seen_at": first[m.device_id],
            "last_seen_at": m.timestamp, "last_value": m.value, "last_sequence": m.sequence,
            "last_run_id": m.run_id,
        }
        for m in latest.values()
    ]  # fmt: skip
    statement = insert(devices).values(rows)
    new = statement.excluded
    newer = new.last_seen_at > devices.c.last_seen_at

    def latest_of(column: str):
        return case((newer, getattr(new, column)), else_=getattr(devices.c, column))

    conn.execute(
        statement.on_conflict_do_update(
            index_elements=["device_id"],
            set_={
                "first_seen_at": func.least(devices.c.first_seen_at, new.first_seen_at),
                **{
                    column: latest_of(column)
                    for column in (
                        "last_seen_at",
                        "last_value",
                        "last_sequence",
                        "last_run_id",
                        "site_id",
                        "worker_id",
                        "unit",
                    )
                },
            },
        )
    )


def store_alerts(conn: Connection, items: list[Received[AlertEvent]]) -> WriteResult:
    """Apply alert transitions; each one is idempotent (the caller commits)."""
    stored = 0
    for item in items:
        stored += _store_alert(conn, item)
    return WriteResult(stored=stored, duplicates=len(items) - stored)


def _store_alert(conn: Connection, item: Received[AlertEvent]) -> int:
    a = item.event
    raised = a.state is AlertState.RAISED
    row = {
        "alert_id": a.alert_id, "rule_id": a.rule_id, "severity": a.severity.value,
        "state": a.state.value, "device_id": a.device_id, "device_type": a.device_type,
        "domain": a.domain, "metric": a.metric, "unit": a.unit, "run_id": a.run_id,
        "condition": a.condition.value, "threshold": a.threshold,
        "received_at": item.received_at,
    }  # fmt: skip
    prefix = "raised" if raised else "cleared"
    row |= {
        f"{prefix}_value": a.value,
        f"{prefix}_event_id": a.measurement_event_id,
        f"{prefix}_at": a.at,
    }
    statement = insert(alerts).values(row)
    if raised:
        # Fill in the opening of an alert; never reopen one that is already cleared.
        update = {
            "raised_value": statement.excluded.raised_value,
            "raised_event_id": statement.excluded.raised_event_id,
            "raised_at": statement.excluded.raised_at,
        }
        changed = alerts.c.raised_at.is_(None)
    else:
        update = {
            "state": AlertState.CLEARED.value,
            "cleared_value": statement.excluded.cleared_value,
            "cleared_event_id": statement.excluded.cleared_event_id,
            "cleared_at": statement.excluded.cleared_at,
        }
        changed = alerts.c.cleared_at.is_(None)
    result = conn.execute(
        statement.on_conflict_do_update(
            index_elements=["alert_id"], set_=update, where=changed
        ).returning(alerts.c.alert_id)
    )
    return len(result.all())
