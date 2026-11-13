"""Runs (one per `stack up`) and overall statistics."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from monitoring_mqtt.api import schemas
from monitoring_mqtt.api.deps import StoreDep
from monitoring_mqtt.api.routes.common import NOT_FOUND, UNAVAILABLE, Limit

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


@router.get("/stats", summary="Totals and storage delays")
def stats(store: StoreDep) -> schemas.Stats:
    """How much is stored, open alerts, and how long events take to reach the database."""
    return schemas.Stats.model_validate(store.stats())
