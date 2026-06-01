"""add ev_soc_pct column to readings

Revision ID: 0005_add_ev_soc_pct
Revises: 0004_add_etrel_power_w
Create Date: 2026-06-01 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_add_ev_soc_pct"
down_revision: str | None = "0004_add_etrel_power_w"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("readings", sa.Column("ev_soc_pct", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("readings", "ev_soc_pct")
