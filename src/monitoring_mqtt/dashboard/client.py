"""Typed HTTP client for the API (the dashboard's only data source).

Lists are paginated by the API; ``_collect`` follows ``next_cursor`` up to a limit, so a
page never waits for an unbounded history. When the API cannot answer (down, or its
database down: 503), every call raises ``ApiUnavailableError``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

PAGE_SIZE = 1000  # the API's maximum
DEFAULT_MAX_ITEMS = 5000


class ApiUnavailableError(RuntimeError):
    """The API (or its database) cannot answer right now."""


class ApiError(RuntimeError):
    """The API rejected the request (a bug in the dashboard, not an outage)."""


ALIASES = {"since": "from", "until": "to"}  # Python names of the API's time filters


def _params(params: dict[str, Any]) -> dict[str, Any]:
    clean = {}
    for key, value in params.items():
        if value is None:
            continue
        clean[ALIASES.get(key, key)] = value.isoformat() if isinstance(value, datetime) else value
    return clean


class ApiClient:
    def __init__(
        self, base_url: str, *, timeout: float = 5.0, client: httpx.Client | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = client or httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, **params: Any) -> Any:
        try:
            response = self._http.get(path, params=_params(params))
        except httpx.HTTPError as exc:
            raise ApiUnavailableError(f"API unreachable at {self.base_url}: {exc}") from exc
        if response.status_code == 503:
            raise ApiUnavailableError("the API's database is unavailable")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ApiError(f"GET {path}: {response.status_code} {response.text[:200]}")
        return response.json()

    def _collect(self, path: str, max_items: int, **params: Any) -> list[dict[str, Any]]:
        """Every item of a paginated list, newest first, at most ``max_items``."""
        items: list[dict[str, Any]] = []
        cursor = None
        while len(items) < max_items:
            page = self._get(
                path, **params, limit=min(PAGE_SIZE, max_items - len(items)), cursor=cursor
            )
            items += page["items"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        return items

    # Routes -------------------------------------------------------------------------

    def ready(self) -> bool:
        try:
            response = self._http.get("/ready")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def stats(self) -> dict[str, Any]:
        return self._get("/api/stats")

    def catalog(self) -> dict[str, Any]:
        return self._get("/api/catalog")

    def runs(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._get("/api/runs", limit=limit)

    def run(self, run_id: str) -> dict[str, Any] | None:
        return self._get(f"/api/runs/{run_id}")

    def devices(self, max_items: int = DEFAULT_MAX_ITEMS, **filters: Any) -> list[dict]:
        return self._collect("/api/devices", max_items, **filters)

    def device(self, device_id: str) -> dict[str, Any] | None:
        return self._get(f"/api/devices/{device_id}")

    def measurements(self, max_items: int = DEFAULT_MAX_ITEMS, **filters: Any) -> list[dict]:
        return self._collect("/api/measurements", max_items, **filters)

    def series(self, **filters: Any) -> dict[str, Any]:
        return self._get("/api/measurements/series", **filters)

    def alerts(self, max_items: int = DEFAULT_MAX_ITEMS, **filters: Any) -> list[dict]:
        return self._collect("/api/alerts", max_items, **filters)
