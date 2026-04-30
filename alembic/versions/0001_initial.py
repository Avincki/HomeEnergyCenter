"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-30 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "readings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("battery_soc_pct", sa.Float(), nullable=True),
        sa.Column("battery_power_w", sa.Float(), nullable=True),
        sa.Column("house_consumption_w", sa.Float(), nullable=True),
        sa.Column("production_w", sa.Float(), nullable=True),
        sa.Column("grid_feed_in_w", sa.Float(), nullable=True),
        sa.Column("car_charger_w", sa.Float(), nullable=True),
        sa.Column("p1_active_power_w", sa.Float(), nullable=True),
        sa.Column("small_solar_w", sa.Float(), nullable=True),
        sa.Column("injection_price_eur_per_kwh", sa.Float(), nullable=True),
        sa.Column("consumption_price_eur_per_kwh", sa.Float(), nullable=True),
    )
    op.create_index("ix_readings_timestamp", "readings", ["timestamp"])

    op.create_table(
        "decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=8), nullable=False),
        sa.Column("rule_fired", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("state_changed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("manual_override", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("override_mode", sa.String(length=16), nullable=True),
        sa.Column("forecast_end_soc_pct", sa.Float(), nullable=True),
    )
    op.create_index("ix_decisions_timestamp", "decisions", ["timestamp"])

    op.create_table(
        "source_status",
        sa.Column("source_name", sa.String(length=32), primary_key=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("last_payload", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("source_status")
    op.drop_index("ix_decisions_timestamp", table_name="decisions")
    op.drop_table("decisions")
    op.drop_index("ix_readings_timestamp", table_name="readings")
    op.drop_table("readings")
