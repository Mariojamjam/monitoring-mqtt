"""Devices seen in the stored history."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from monitoring_mqtt.api import schemas
from monitoring_mqtt.api.deps import StoreDep
from monitoring_mqtt.api.routes.common import NOT_FOUND, UNAVAILABLE, Cursor, Limit

router = APIRouter(prefix="/devices", tags=["devices"], responses=UNAVAILABLE)


@router.get("", summary="List devices with their latest value")
def list_devices(
    store: StoreDep,
    domain: Annotated[str | None, Query(description="e.g. poultry")] = None,
    device_type: Annotated[str | None, Query(description="e.g. ammonia_detector")] = None,
    run_id: Annotated[str | None, Query(description="Devices whose latest run is this")] = None,
    limit: Limit = 100,
    cursor: Cursor = None,
) -> schemas.Page[schemas.Device]:
    """Ordered by `device_id`. `open_alerts` counts the device's alerts still raised."""
    page = store.list_devices(
        domain=domain, device_type=device_type, run_id=run_id, limit=limit, cursor=cursor
    )
    return schemas.Page(items=[schemas.Device.model_validate(r) for r in page.items],
                        next_cursor=page.next_cursor)  # fmt: skip


@router.get("/{device_id}", summary="One device", responses=NOT_FOUND)
def get_device(device_id: str, store: StoreDep) -> schemas.DeviceDetail:
    """The device, how many measurements are stored and its open alerts."""
    row = store.get_device(device_id)
    if row is None:
        raise HTTPException(404, f"device {device_id} not found")
    alerts = [schemas.Alert.of(a) for a in row.pop("open_alerts")]
    return schemas.DeviceDetail.model_validate({**row, "open_alerts": alerts})
