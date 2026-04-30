"""Alembic environment for the Energy Orchestrator.

DB URL resolution order:
  1. ``EO_DB_URL`` environment variable (full SQLAlchemy URL).
  2. ``EO_SQLITE_PATH`` environment variable (filesystem path; converted via aiosqlite).
  3. ``data/orchestrator.db`` relative to the working directory.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from energy_orchestrator.data.database import make_sqlite_url
from energy_orchestrator.data.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_db_url() -> str:
    explicit = os.environ.get("EO_DB_URL")
    if explicit:
        return explicit
    sqlite_path = os.environ.get("EO_SQLITE_PATH", "data/orchestrator.db")
    return make_sqlite_url(sqlite_path)


def run_migrations_offline() -> None:
    """Emit SQL without an actual DB connection."""
    url = _resolve_db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = _resolve_db_url()
    connectable = async_engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
