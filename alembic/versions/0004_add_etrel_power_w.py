"""add etrel_power_w column to readings

Revision ID: 0004_add_etrel_power_w
Revises: 0003_persist_prices_and_solar
Create Date: 2026-05-07 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_add_etrel_power_w"
down_revision: str | None = "0003_persist_prices_and_solar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("readings", sa.Column("etrel_power_w", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("readings", "etrel_power_w")
