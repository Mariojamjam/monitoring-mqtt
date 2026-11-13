"""Database settings read from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from monitoring_mqtt.core.config import ConfigError, env_int

DEFAULT_URL = "postgresql+psycopg://monitoring:monitoring@localhost:5432/monitoring"


@dataclass(frozen=True)
class DatabaseSettings:
    url: str = DEFAULT_URL
    pool_size: int = 5
    connect_timeout: int = 3  # seconds; fail fast so the API can answer 503

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DatabaseSettings:
        env = os.environ if env is None else env
        url = env.get("DATABASE_URL", "").strip() or DEFAULT_URL
        if not url.startswith("postgresql"):
            raise ConfigError("DATABASE_URL must be a postgresql:// URL")
        return cls(
            url=url,
            pool_size=env_int(env, "DATABASE_POOL_SIZE", cls.pool_size, 1, 100),
            connect_timeout=env_int(env, "DATABASE_CONNECT_TIMEOUT", cls.connect_timeout, 1, 60),
        )
