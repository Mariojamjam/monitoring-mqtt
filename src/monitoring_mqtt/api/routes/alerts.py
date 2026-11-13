"""Stored alerts."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query

from monitoring_mqtt.api import schemas
from monitoring_mqtt.api.deps import StoreDep
from monitoring_mqtt.api.routes.common import NOT_FOUND, UNAVAILABLE, Cursor, Limit, Since, Until

router = APIRouter(prefix="/alerts", tags=["alerts"], responses=UNAVAILABLE)


@router.get("", summary="List alerts")
def list_alerts(
    store: StoreDep,
    state: Annotated[
        Literal["active", "cleared"] | None, Query(description="`active` = still raised")
    ] = None,
    severity: Literal["warning", "critical"] | None = None,
    domain: str | None = None,
    device_id: str | None = None,
    rule_id: str | None = None,
    run_id: str | None = None,
    since: Since = None,
    until: Until = None,
    limit: Limit = 100,
    cursor: Cursor = None,
) -> schemas.Page[schemas.Alert]:
    """Newest first, by the time the alert was raised. `from`/`to` filter on that time too."""
    page = store.list_alerts(
        state={"active": "raised", "cleared": "cleared"}.get(state or ""),
        severity=severity, domain=domain, device_id=device_id, rule_id=rule_id,
        run_id=run_id, since=since, until=until, limit=limit, cursor=cursor,
    )  # fmt: skip
    return schemas.Page(items=[schemas.Alert.of(r) for r in page.items],
                        next_cursor=page.next_cursor)  # fmt: skip


@router.get("/{alert_id}", summary="One alert", responses=NOT_FOUND)
def get_alert(alert_id: uuid.UUID, store: StoreDep) -> schemas.Alert:
    row = store.get_alert(alert_id)
    if row is None:
        raise HTTPException(404, f"alert {alert_id} not found")
    return schemas.Alert.of(row)
