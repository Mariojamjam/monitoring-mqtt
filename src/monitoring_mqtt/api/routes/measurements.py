"""Stored measurements and time series."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from monitoring_mqtt.api import schemas
from monitoring_mqtt.api.deps import StoreDep
from monitoring_mqtt.api.routes.common import UNAVAILABLE, Cursor, Limit, Since, Until
from monitoring_mqtt.core.durations import parse_duration

router = APIRouter(prefix="/measurements", tags=["measurements"], responses=UNAVAILABLE)

MIN_BUCKET_SECONDS = 1.0
MAX_POINTS = 2000


@router.get("", summary="List measurements")
def list_measurements(
    store: StoreDep,
    device_id: str | None = None,
    domain: str | None = None,
    metric: str | None = None,
    run_id: str | None = None,
    since: Since = None,
    until: Until = None,
    limit: Limit = 100,
    cursor: Cursor = None,
) -> schemas.Page[schemas.Measurement]:
    """Newest first, by the time the device generated them. Filters combine with AND.
    Each item carries its three times: generated on the device, received by the ingestion
    consumer, stored by the database."""
    page = store.list_measurements(
        device_id=device_id, domain=domain, metric=metric, run_id=run_id,
        since=since, until=until, limit=limit, cursor=cursor,
    )  # fmt: skip
    return schemas.Page(items=[schemas.Measurement.model_validate(r) for r in page.items],
                        next_cursor=page.next_cursor)  # fmt: skip


@router.get("/series", summary="Aggregated time series")
def series(
    store: StoreDep,
    device_id: Annotated[str | None, Query(description="One device")] = None,
    domain: Annotated[str | None, Query(description="Every device of a domain")] = None,
    run_id: Annotated[str | None, Query(description="Only this run")] = None,
    bucket: Annotated[str, Query(description="Bucket size: 10s, 1m, 5m, 1h…")] = "1m",
    since: Since = None,
    until: Until = None,
) -> schemas.Series:
    """Average, minimum, maximum and count per bucket, oldest first (at most 2000
    buckets: the most recent ones). Over one device (`device_id`) or all the devices of a
    domain (`domain`): set exactly one. Ready for charts."""
    if (device_id is None) == (domain is None):
        raise HTTPException(422, "set exactly one of device_id or domain")
    try:
        seconds = parse_duration(bucket)
    except ValueError as exc:
        raise HTTPException(422, f"bucket: {exc}") from exc
    if seconds < MIN_BUCKET_SECONDS:
        raise HTTPException(422, "bucket: at least 1s")
    points = store.series(seconds, device_id=device_id, domain=domain, run_id=run_id,
                          since=since, until=until, limit=MAX_POINTS)  # fmt: skip
    return schemas.Series(
        device_id=device_id,
        domain=domain,
        bucket_seconds=seconds,
        points=[schemas.SeriesPoint.model_validate(p) for p in points],
    )
