"""add large_solar_w column to readings

Revision ID: 0002_add_large_solar_w
Revises: 0001_initial
Create Date: 2026-05-04 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_large_solar_w"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("readings", sa.Column("large_solar_w", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("readings", "large_solar_w")
