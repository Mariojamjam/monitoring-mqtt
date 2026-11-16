"""Runs (one per `stack up`) and overall statistics."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from monitoring_mqtt.api import schemas
from monitoring_mqtt.api.deps import StoreDep
from monitoring_mqtt.api.routes.common import NOT_FOUND, UNAVAILABLE, Limit, Since, Until

router = APIRouter(tags=["runs"], responses=UNAVAILABLE)


@router.get("/runs", summary="List runs")
def list_runs(store: StoreDep, limit: Limit = 100) -> list[schemas.Run]:
    """Every `run_id` seen in the measurements, with what was stored for it. Newest first,
    by the time of the run's first measurement."""
    return [schemas.Run.model_validate(r) for r in store.list_runs(limit=limit)]


@router.get("/runs/{run_id}", summary="One run", responses=NOT_FOUND)
def get_run(run_id: str, store: StoreDep) -> schemas.RunDetail:
    row = store.get_run(run_id)
    if row is None:
        raise HTTPException(404, f"run {run_id} not found")
    return schemas.RunDetail.model_validate(row)


@router.get("/runs/{run_id}/window", summary="One run inside a time window",
            responses=NOT_FOUND)  # fmt: skip
def run_window(run_id: str, store: StoreDep, since: Since = None,
               until: Until = None) -> schemas.RunWindow:  # fmt: skip
    """Counts, delays (average and percentiles) and missing sequence numbers of the run's
    measurements generated in [`from`, `to`), and the alerts raised in it. Experiments use
    it to measure a fixed window after a warm-up. Both bounds are required."""
    if since is None or until is None or since >= until:
        raise HTTPException(422, "set from and to, with from before to")
    if store.get_run(run_id) is None:
        raise HTTPException(404, f"run {run_id} not found")
    return schemas.RunWindow.model_validate(store.run_window(run_id, since, until))


@router.get("/stats", summary="Totals and storage delays")
def stats(store: StoreDep) -> schemas.Stats:
    """How much is stored, open alerts, and how long events take to reach the database."""
    return schemas.Stats.model_validate(store.stats())
