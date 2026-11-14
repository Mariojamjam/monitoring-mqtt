"""Read side: queries over the stored history.

Lists are paginated with a **cursor** (keyset pagination): each page ends with an opaque
token naming its last row, and the next page starts strictly after it. Unlike an offset,
this stays fast on large tables and does not skip or repeat rows while new data arrives.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, and_, func, literal, or_, select, tuple_
from sqlalchemy.dialects.postgresql import INTERVAL

from monitoring_mqtt.storage.schema import alerts, devices, measurements, runs

STATS_DELAY_SAMPLE = 1000  # latest measurements used for the delay averages


class InvalidCursorError(ValueError):
    """The cursor was not produced by this API (or the query changed)."""


def encode_cursor(*values: Any) -> str:
    raw = json.dumps([v.isoformat() if isinstance(v, datetime) else str(v) for v in values])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, size: int) -> list[str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursorError("invalid cursor") from exc
    if not isinstance(values, list) or len(values) != size:
        raise InvalidCursorError("invalid cursor")
    return [str(v) for v in values]


@dataclass(frozen=True)
class Page:
    items: list[dict[str, Any]]
    next_cursor: str | None


def _rows(result) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in result]


# Devices --------------------------------------------------------------------------------


def list_devices(
    conn: Connection,
    *,
    domain: str | None = None,
    device_type: str | None = None,
    run_id: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> Page:
    open_alerts = (
        select(func.count())
        .where(alerts.c.device_id == devices.c.device_id, alerts.c.state == "raised")
        .scalar_subquery()
    )
    query = select(devices, open_alerts.label("open_alerts")).order_by(devices.c.device_id)
    if domain:
        query = query.where(devices.c.domain == domain)
    if device_type:
        query = query.where(devices.c.device_type == device_type)
    if run_id:
        query = query.where(devices.c.last_run_id == run_id)
    if cursor:
        (after,) = decode_cursor(cursor, 1)
        query = query.where(devices.c.device_id > after)
    items = _rows(conn.execute(query.limit(limit + 1)))
    more = len(items) > limit
    items = items[:limit]
    return Page(items, encode_cursor(items[-1]["device_id"]) if more else None)


def get_device(conn: Connection, device_id: str) -> dict[str, Any] | None:
    row = conn.execute(select(devices).where(devices.c.device_id == device_id)).first()
    if row is None:
        return None
    device = dict(row._mapping)
    counts = conn.execute(
        select(func.count(), func.min(measurements.c.generated_at)).where(
            measurements.c.device_id == device_id
        )
    ).one()
    device["measurements"] = counts[0]
    device["open_alerts"] = _rows(
        conn.execute(
            select(alerts)
            .where(alerts.c.device_id == device_id, alerts.c.state == "raised")
            .order_by(alerts.c.raised_at.desc())
        )
    )
    return device


# Measurements ---------------------------------------------------------------------------


def list_measurements(
    conn: Connection,
    *,
    device_id: str | None = None,
    domain: str | None = None,
    metric: str | None = None,
    run_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> Page:
    """Newest first (by generation time)."""
    m = measurements.c
    query = select(measurements).order_by(m.generated_at.desc(), m.id.desc())
    for column, value in ((m.device_id, device_id), (m.domain, domain), (m.metric, metric),
                          (m.run_id, run_id)):  # fmt: skip
        if value:
            query = query.where(column == value)
    if since:
        query = query.where(m.generated_at >= since)
    if until:
        query = query.where(m.generated_at < until)
    if cursor:
        at, row_id = decode_cursor(cursor, 2)
        try:
            key = (datetime.fromisoformat(at), int(row_id))
        except ValueError as exc:
            raise InvalidCursorError("invalid cursor") from exc
        query = query.where(tuple_(m.generated_at, m.id) < tuple_(*key))
    items = _rows(conn.execute(query.limit(limit + 1)))
    more = len(items) > limit
    items = items[:limit]
    last = items[-1] if items else None
    return Page(items, encode_cursor(last["generated_at"], last["id"]) if more and last else None)


def series(
    conn: Connection,
    bucket_seconds: float,
    *,
    device_id: str | None = None,
    domain: str | None = None,
    run_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Average, minimum, maximum and count per time bucket, oldest first.

    Over one device or every device of a domain (the filters combine with AND).
    """
    m = measurements.c
    bucket = func.date_bin(
        literal(f"{bucket_seconds} seconds").cast(INTERVAL()),
        m.generated_at,
        literal(datetime(2000, 1, 1).isoformat()).cast(m.generated_at.type),
    ).label("bucket_start")
    query = (
        select(
            bucket,
            func.count().label("count"),
            func.avg(m.value).label("avg"),
            func.min(m.value).label("min"),
            func.max(m.value).label("max"),
        )
        .group_by(bucket)
        .order_by(bucket.desc())
        .limit(limit)
    )
    for column, value in ((m.device_id, device_id), (m.domain, domain), (m.run_id, run_id)):
        if value:
            query = query.where(column == value)
    if since:
        query = query.where(m.generated_at >= since)
    if until:
        query = query.where(m.generated_at < until)
    return list(reversed(_rows(conn.execute(query))))


# Alerts ---------------------------------------------------------------------------------

_OPENED_AT = func.coalesce(alerts.c.raised_at, alerts.c.cleared_at)


def list_alerts(
    conn: Connection,
    *,
    state: str | None = None,
    severity: str | None = None,
    domain: str | None = None,
    device_id: str | None = None,
    rule_id: str | None = None,
    run_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> Page:
    """Newest first (by the time the alert was raised)."""
    a = alerts.c
    opened = _OPENED_AT.label("opened_at")
    query = select(alerts, opened).order_by(opened.desc(), a.alert_id.desc())
    filters = [
        (a.state, state), (a.severity, severity), (a.domain, domain),
        (a.device_id, device_id), (a.rule_id, rule_id), (a.run_id, run_id),
    ]  # fmt: skip
    for column, value in filters:
        if value:
            query = query.where(column == value)
    if since:
        query = query.where(since <= _OPENED_AT)
    if until:
        query = query.where(until > _OPENED_AT)
    if cursor:
        at, alert_id = decode_cursor(cursor, 2)
        try:
            key_at, key_id = datetime.fromisoformat(at), uuid.UUID(alert_id)
        except ValueError as exc:
            raise InvalidCursorError("invalid cursor") from exc
        query = query.where(
            or_(key_at > _OPENED_AT, and_(key_at == _OPENED_AT, a.alert_id < key_id))
        )
    items = _rows(conn.execute(query.limit(limit + 1)))
    more = len(items) > limit
    items = items[:limit]
    last = items[-1] if items else None
    return Page(
        items, encode_cursor(last["opened_at"], last["alert_id"]) if more and last else None
    )


def get_alert(conn: Connection, alert_id: uuid.UUID) -> dict[str, Any] | None:
    row = conn.execute(
        select(alerts, _OPENED_AT.label("opened_at")).where(alerts.c.alert_id == alert_id)
    ).first()
    return dict(row._mapping) if row else None


# Runs and statistics ----------------------------------------------------------------------


def _run_counts(conn: Connection, run_ids: list[str]) -> dict[str, dict[str, int]]:
    if not run_ids:
        return {}
    m = measurements.c
    counts = {
        row.run_id: {"measurements": row.n, "devices": row.d, "alerts": 0}
        for row in conn.execute(
            select(m.run_id, func.count().label("n"), func.count(m.device_id.distinct()).label("d"))
            .where(m.run_id.in_(run_ids))
            .group_by(m.run_id)
        )
    }
    for row in conn.execute(
        select(alerts.c.run_id, func.count().label("n"))
        .where(alerts.c.run_id.in_(run_ids))
        .group_by(alerts.c.run_id)
    ):
        counts.setdefault(row.run_id, {"measurements": 0, "devices": 0, "alerts": 0})
        counts[row.run_id]["alerts"] = row.n
    return counts


def list_runs(conn: Connection, *, limit: int = 100) -> list[dict[str, Any]]:
    items = _rows(conn.execute(select(runs).order_by(runs.c.first_seen_at.desc()).limit(limit)))
    counts = _run_counts(conn, [r["run_id"] for r in items])
    empty = {"measurements": 0, "devices": 0, "alerts": 0}
    return [{**r, **counts.get(r["run_id"], empty)} for r in items]


def _ms(seconds: Any) -> float | None:
    return None if seconds is None else round(float(seconds) * 1000, 1)


def get_run(conn: Connection, run_id: str) -> dict[str, Any] | None:
    """The run with what was stored for it and how the system behaved during it."""
    row = conn.execute(select(runs).where(runs.c.run_id == run_id)).first()
    if row is None:
        return None
    run = dict(row._mapping)
    run.update(_run_counts(conn, [run_id]).get(run_id, {"measurements": 0, "devices": 0,
                                                          "alerts": 0}))  # fmt: skip
    m = measurements.c
    run["domains"] = _rows(
        conn.execute(
            select(m.domain, func.count().label("measurements"),
                   func.count(m.device_id.distinct()).label("devices"))
            .where(m.run_id == run_id)
            .group_by(m.domain)
            .order_by(m.domain)
        )
    )  # fmt: skip
    duration = (run["last_seen_at"] - run["first_seen_at"]).total_seconds()
    run["duration_seconds"] = round(duration, 3)
    run["measurements_per_second"] = round(run["measurements"] / duration, 3) if duration else None

    to_broker = func.extract("epoch", m.received_at - m.generated_at)
    to_database = func.extract("epoch", m.stored_at - m.generated_at)
    delays = conn.execute(
        select(
            func.avg(to_broker),
            func.avg(to_database),
            func.percentile_cont(0.95).within_group(to_database),
            func.max(to_database),
        ).where(m.run_id == run_id)
    ).one()
    run["generated_to_received_ms"] = _ms(delays[0])
    run["generated_to_stored_ms"] = _ms(delays[1])
    run["generated_to_stored_p95_ms"] = _ms(delays[2])
    run["generated_to_stored_max_ms"] = _ms(delays[3])

    # Missing sequence numbers per device: between its lowest and highest stored sequence.
    # A device restarted within the run starts again at 1, so its gaps may be undercounted.
    per_device = (
        select(
            (func.max(m.sequence) - func.min(m.sequence) + 1
             - func.count(m.sequence.distinct())).label("missing")
        )
        .where(m.run_id == run_id)
        .group_by(m.device_id)
        .subquery()
    )  # fmt: skip
    missing = conn.execute(
        select(func.coalesce(func.sum(func.greatest(per_device.c.missing, 0)), 0))
    ).scalar_one()
    run["sequence_gaps"] = int(missing)

    a = alerts.c
    run["alerts_by_severity"] = {
        r.severity: r.n
        for r in conn.execute(
            select(a.severity, func.count().label("n"))
            .where(a.run_id == run_id)
            .group_by(a.severity)
        )
    }
    return run


def stats(conn: Connection) -> dict[str, Any]:
    m = measurements.c
    totals = {
        "measurements": conn.execute(select(func.count()).select_from(measurements)).scalar_one(),
        "devices": conn.execute(select(func.count()).select_from(devices)).scalar_one(),
        "runs": conn.execute(select(func.count()).select_from(runs)).scalar_one(),
        "alerts_total": conn.execute(select(func.count()).select_from(alerts)).scalar_one(),
        "alerts_open": conn.execute(
            select(func.count()).select_from(alerts).where(alerts.c.state == "raised")
        ).scalar_one(),
    }
    latest = (
        select(m.generated_at, m.received_at, m.stored_at)
        .order_by(m.id.desc())
        .limit(STATS_DELAY_SAMPLE)
        .subquery()
    )
    seconds = func.extract("epoch", latest.c.stored_at - latest.c.received_at)
    total = func.extract("epoch", latest.c.stored_at - latest.c.generated_at)
    row = conn.execute(
        select(func.max(latest.c.stored_at), func.avg(seconds), func.avg(total))
    ).one()
    totals["last_stored_at"] = row[0]
    totals["received_to_stored_ms"] = None if row[1] is None else round(float(row[1]) * 1000, 1)
    totals["generated_to_stored_ms"] = None if row[2] is None else round(float(row[2]) * 1000, 1)
    return totals
