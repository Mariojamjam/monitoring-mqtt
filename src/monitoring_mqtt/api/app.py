"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from monitoring_mqtt import __version__
from monitoring_mqtt.api import docs
from monitoring_mqtt.api.routes import alerts, devices, health, ingest, measurements, runs
from monitoring_mqtt.storage.queries import InvalidCursorError
from monitoring_mqtt.storage.settings import DatabaseSettings
from monitoring_mqtt.storage.store import StorageUnavailableError, Store

RETRY_AFTER_SECONDS = 2
API_PREFIX = "/api"

DESCRIPTION = """\
History of the Monitoring MQTT lab. The API is the **only** component that talks to the
database.

- **Write side** (`/api/ingest`): the ingestion consumer posts batches of events it received
  from the broker. Every event is stored once (idempotent by id).
- **Read side** (`/api/...`): devices, measurements, alerts and runs, as JSON.

`/health` and `/ready` stay at the root, for container healthchecks.

Lists are paginated with a cursor: pass `next_cursor` back as `cursor` to get the next
page (`null` on the last one). Times are ISO 8601 with a time zone (UTC).

When the database is unavailable, every route that needs it answers **503** with a
`Retry-After` header.
"""

TAGS = [
    {"name": "health", "description": "Liveness and readiness"},
    {"name": "ingest", "description": "Batches of events from the ingestion consumer"},
    {"name": "devices", "description": "Devices seen in the history, with their latest value"},
    {"name": "measurements", "description": "Stored measurements and time series"},
    {"name": "alerts", "description": "Alerts raised by the alert engine"},
    {"name": "runs", "description": "Runs (one per `stack up`) and statistics"},
]


def create_app(store: object | None = None) -> FastAPI:
    """Build the app; ``store`` replaces the database (tests, OpenAPI export)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own = store is None
        app.state.store = Store.from_settings(DatabaseSettings.from_env()) if own else store
        try:
            yield
        finally:
            if own:
                app.state.store.close()

    app = FastAPI(
        title="Monitoring MQTT",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    if store is not None:
        app.state.store = store

    @app.exception_handler(StorageUnavailableError)
    async def storage_unavailable(request: Request, exc: StorageUnavailableError) -> JSONResponse:
        return JSONResponse(
            {"detail": "database unavailable"},
            status_code=503,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )

    @app.exception_handler(InvalidCursorError)
    async def invalid_cursor(request: Request, exc: InvalidCursorError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # Health stays at the root (container healthchecks); the API contract is under /api.
    app.include_router(health.router)
    for router in (ingest.router, devices.router, measurements.router, alerts.router,
                   runs.router):  # fmt: skip
        app.include_router(router, prefix=API_PREFIX)
    docs.install(app)
    return app
