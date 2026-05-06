"""persist day-ahead prices and forecast.solar points

Revision ID: 0003_persist_prices_and_solar
Revises: 0002_add_large_solar_w
Create Date: 2026-05-05 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_persist_prices_and_solar"
down_revision: str | None = "0002_add_large_solar_w"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "price_points",
        sa.Column("timestamp", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("consumption_eur_per_kwh", sa.Float(), nullable=True),
        sa.Column("injection_eur_per_kwh", sa.Float(), nullable=True),
    )

    op.create_table(
        "solar_forecast_points",
        sa.Column("timestamp", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("plane", sa.String(length=64), primary_key=True),
        sa.Column("watts", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("solar_forecast_points")
    op.drop_table("price_points")
