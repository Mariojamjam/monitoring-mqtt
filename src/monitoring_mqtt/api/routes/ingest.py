"""Write side: batches of events from the ingestion consumer."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ValidationError

from monitoring_mqtt.api.deps import StoreDep
from monitoring_mqtt.core.alerts import AlertEvent
from monitoring_mqtt.core.ingest import MAX_BATCH_ITEMS, IngestBatch, IngestResult, Rejected
from monitoring_mqtt.core.models import Measurement
from monitoring_mqtt.storage.writes import Received, WriteResult

router = APIRouter(prefix="/ingest", tags=["ingest"])

RESPONSES: dict[int | str, dict[str, Any]] = {
    413: {"description": f"More than {MAX_BATCH_ITEMS} items in one batch"},
    503: {"description": "Database unavailable; retry later (see Retry-After)"},
}


def _reason(exc: ValidationError) -> str:
    error = exc.errors()[0]
    where = ".".join(str(part) for part in error["loc"]) or "event"
    return f"{where}: {error['msg']}"


def _ingest[T: BaseModel](
    batch: IngestBatch, model: type[T], store: Callable[[list[Received[T]]], WriteResult]
) -> IngestResult:
    if len(batch.items) > MAX_BATCH_ITEMS:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"at most {MAX_BATCH_ITEMS} items per batch, got {len(batch.items)}",
        )
    valid: list[Received[T]] = []
    rejected: list[Rejected] = []
    for index, item in enumerate(batch.items):
        try:
            valid.append(Received(model.model_validate(item.event), item.received_at))
        except ValidationError as exc:
            rejected.append(Rejected(index=index, reason=_reason(exc)))
    result = store(valid) if valid else WriteResult(0, 0)
    return IngestResult(
        received=len(batch.items),
        stored=result.stored,
        duplicates=result.duplicates,
        rejected=rejected,
    )


@router.post(
    "/measurements",
    summary="Store a batch of measurements",
    responses=RESPONSES,
    openapi_extra={"x-event-schema": "Measurement"},
)
def ingest_measurements(batch: IngestBatch, store: StoreDep) -> IngestResult:
    """Each item's ``event`` is a telemetry payload (schema `Measurement`).

    Valid items are stored once per `event_id` (repeats count as `duplicates`); invalid
    items are listed in `rejected` with the reason, and do not prevent the others.
    """
    return _ingest(batch, Measurement, store.store_measurements)


@router.post(
    "/alerts",
    summary="Store a batch of alert events",
    responses=RESPONSES,
    openapi_extra={"x-event-schema": "AlertEvent"},
)
def ingest_alerts(batch: IngestBatch, store: StoreDep) -> IngestResult:
    """Each item's ``event`` is an alert event (schema `AlertEvent`).

    `raised` opens the alert and `cleared` closes it, in any order; repeats are
    `duplicates`.
    """
    return _ingest(batch, AlertEvent, store.store_alerts)
