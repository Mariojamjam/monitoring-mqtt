"""Request dependencies: the storage facade and the catalog, replaceable in tests."""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import Depends, Request

from monitoring_mqtt.api.schemas import Catalog
from monitoring_mqtt.core.alerts import AlertEvent
from monitoring_mqtt.core.models import Measurement
from monitoring_mqtt.storage.writes import Received, WriteResult


class StorePort(Protocol):
    """What the API needs from storage (``storage.store.Store`` in production)."""

    def ping(self) -> None: ...
    def store_measurements(self, items: list[Received[Measurement]]) -> WriteResult: ...
    def store_alerts(self, items: list[Received[AlertEvent]]) -> WriteResult: ...


def get_store(request: Request) -> StorePort:
    return request.app.state.store


StoreDep = Annotated[StorePort, Depends(get_store)]


def get_catalog(request: Request) -> Catalog:
    """Loaded on first use, then kept (the files do not change while the API runs)."""
    state = request.app.state
    if getattr(state, "catalog", None) is None:
        state.catalog = state.load_catalog()
    return state.catalog


CatalogDep = Annotated[Catalog, Depends(get_catalog)]
