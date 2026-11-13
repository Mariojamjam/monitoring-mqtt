"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from monitoring_mqtt.api.deps import StoreDep

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str


@router.get("/health", summary="The API process is running")
def health() -> Health:
    """Never touches the database: used by the container healthcheck."""
    return Health(status="ok")


@router.get(
    "/ready",
    summary="The API can reach the database",
    responses={503: {"description": "Database unavailable"}},
)
def ready(store: StoreDep) -> Health:
    store.ping()  # raises StorageUnavailableError → 503
    return Health(status="ready")
