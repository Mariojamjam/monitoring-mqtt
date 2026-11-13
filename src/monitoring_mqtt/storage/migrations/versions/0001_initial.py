"""Initial schema: runs, devices, measurements, alerts.

Revision ID: 0001
Revises:
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = False, **kwargs) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, **kwargs)


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(64), nullable=False),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
        sa.PrimaryKeyConstraint("run_id", name="pk_runs"),
    )
    op.create_table(
        "devices",
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("device_type", sa.String(64), nullable=False),
        sa.Column("domain", sa.String(64), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("site_id", sa.String(64), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=False),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
        sa.Column("last_value", sa.Float(), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_run_id", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("device_id", name="pk_devices"),
    )
    op.create_table(
        "measurements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("domain", sa.String(64), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("site_id", sa.String(64), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        _ts("generated_at"),
        _ts("received_at"),
        _ts("stored_at", server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.device_id"], name="fk_measurements_device_id"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_measurements"),
        sa.UniqueConstraint("event_id", name="uq_measurements_event_id"),
    )
    op.create_index(
        "ix_measurements_device_generated", "measurements", ["device_id", "generated_at"]
    )
    op.create_index("ix_measurements_domain_generated", "measurements", ["domain", "generated_at"])
    op.create_index("ix_measurements_run", "measurements", ["run_id"])
    op.create_table(
        "alerts",
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("device_type", sa.String(64), nullable=False),
        sa.Column("domain", sa.String(64), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("condition", sa.String(8), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("raised_value", sa.Float(), nullable=True),
        sa.Column("raised_event_id", sa.Uuid(), nullable=True),
        _ts("raised_at", nullable=True),
        sa.Column("cleared_value", sa.Float(), nullable=True),
        sa.Column("cleared_event_id", sa.Uuid(), nullable=True),
        _ts("cleared_at", nullable=True),
        _ts("received_at"),
        _ts("stored_at", server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("alert_id", name="pk_alerts"),
    )
    op.create_index("ix_alerts_state", "alerts", ["state"])
    op.create_index("ix_alerts_device_raised", "alerts", ["device_id", "raised_at"])


def downgrade() -> None:
    op.drop_table("alerts")
    op.drop_table("measurements")
    op.drop_table("devices")
    op.drop_table("runs")
