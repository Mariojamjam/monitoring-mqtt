"""Database schema (SQLAlchemy Core). Changes need an Alembic migration.

Three times are kept apart for every measurement: ``generated_at`` (the payload timestamp,
on the device), ``received_at`` (when the ingestion consumer got it from the broker) and
``stored_at`` (when the database wrote it). Their differences show the delay of each hop.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    MetaData,
    String,
    Table,
    Uuid,
    func,
)

metadata = MetaData(
    naming_convention={
        "pk": "pk_%(table_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ix": "ix_%(table_name)s_%(column_0_label)s",
    }
)


def _timestamp(name: str, nullable: bool = False, **kwargs) -> Column:
    return Column(name, DateTime(timezone=True), nullable=nullable, **kwargs)


runs = Table(
    "runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    _timestamp("first_seen_at"),
    _timestamp("last_seen_at"),
)

devices = Table(
    "devices",
    metadata,
    Column("device_id", String(128), primary_key=True),
    Column("device_type", String(64), nullable=False),
    Column("domain", String(64), nullable=False),
    Column("metric", String(64), nullable=False),
    Column("unit", String(32)),
    Column("site_id", String(64), nullable=False),
    Column("worker_id", String(64), nullable=False),
    _timestamp("first_seen_at"),
    _timestamp("last_seen_at"),
    Column("last_value", Float, nullable=False),
    Column("last_sequence", BigInteger, nullable=False),
    Column("last_run_id", String(64), nullable=False),
)

measurements = Table(
    "measurements",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("event_id", Uuid, nullable=False, unique=True),
    Column("run_id", String(64), nullable=False),
    Column("device_id", String(128), ForeignKey("devices.device_id"), nullable=False),
    Column("domain", String(64), nullable=False),
    Column("metric", String(64), nullable=False),
    Column("site_id", String(64), nullable=False),
    Column("value", Float, nullable=False),
    Column("unit", String(32)),
    Column("sequence", BigInteger, nullable=False),
    _timestamp("generated_at"),
    _timestamp("received_at"),
    _timestamp("stored_at", server_default=func.now()),
    Index("ix_measurements_device_generated", "device_id", "generated_at"),
    Index("ix_measurements_domain_generated", "domain", "generated_at"),
    Index("ix_measurements_run", "run_id"),
)

alerts = Table(
    "alerts",
    metadata,
    Column("alert_id", Uuid, primary_key=True),
    Column("rule_id", String(64), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("state", String(16), nullable=False),
    Column("device_id", String(128), nullable=False),
    Column("device_type", String(64), nullable=False),
    Column("domain", String(64), nullable=False),
    Column("metric", String(64), nullable=False),
    Column("unit", String(32)),
    Column("run_id", String(64), nullable=False),
    Column("condition", String(8), nullable=False),
    Column("threshold", Float, nullable=False),
    # No foreign key to measurements: an alert may be stored before the measurement that
    # raised it (both travel through the broker independently).
    Column("raised_value", Float),
    Column("raised_event_id", Uuid),
    _timestamp("raised_at", nullable=True),
    Column("cleared_value", Float),
    Column("cleared_event_id", Uuid),
    _timestamp("cleared_at", nullable=True),
    _timestamp("received_at"),
    _timestamp("stored_at", server_default=func.now()),
    Index("ix_alerts_state", "state"),
    Index("ix_alerts_device_raised", "device_id", "raised_at"),
)

__all__ = ["alerts", "devices", "measurements", "metadata", "runs"]
