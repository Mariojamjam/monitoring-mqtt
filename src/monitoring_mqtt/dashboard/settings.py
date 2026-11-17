"""Dashboard settings, from the environment (set by Compose)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_REFRESH_SECONDS = 5.0


@dataclass(frozen=True)
class DashboardSettings:
    api_url: str = DEFAULT_API_URL
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS  # 0 turns live refresh off

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> DashboardSettings:
        raw = env.get("DASHBOARD_REFRESH_SECONDS", "").strip()
        try:
            refresh = float(raw) if raw else DEFAULT_REFRESH_SECONDS
        except ValueError as exc:
            raise ValueError(f"DASHBOARD_REFRESH_SECONDS: not a number: {raw!r}") from exc
        if refresh < 0:
            raise ValueError("DASHBOARD_REFRESH_SECONDS must not be negative")
        api_url = env.get("API_URL", "").strip() or DEFAULT_API_URL
        return cls(api_url=api_url.rstrip("/"), refresh_seconds=refresh)
