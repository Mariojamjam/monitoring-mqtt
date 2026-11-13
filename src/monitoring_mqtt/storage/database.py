"""Engine and readiness check."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, text

from monitoring_mqtt.storage.settings import DatabaseSettings


def make_engine(settings: DatabaseSettings) -> Engine:
    return create_engine(
        settings.url,
        pool_size=settings.pool_size,
        pool_pre_ping=True,  # a restarted database must not leave dead connections behind
        connect_args={"connect_timeout": settings.connect_timeout},
    )


def ping(engine: Engine) -> None:
    """Raise if the database cannot answer a trivial query."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
