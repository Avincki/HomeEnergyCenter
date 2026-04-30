from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from energy_orchestrator.data.models import Base


def make_sqlite_url(path: str | Path) -> str:
    """Build an aiosqlite URL from a filesystem path or the literal ':memory:'."""
    if str(path) == ":memory:":
        return "sqlite+aiosqlite:///:memory:"
    return f"sqlite+aiosqlite:///{Path(path).resolve().as_posix()}"


def create_engine(sqlite_path: str | Path, *, echo: bool = False) -> AsyncEngine:
    # SQLite won't create parent directories; do it for relative paths like data/orchestrator.db.
    if str(sqlite_path) != ":memory:":
        parent = Path(sqlite_path).resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(make_sqlite_url(sqlite_path), echo=echo, future=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_schema(engine: AsyncEngine) -> None:
    """Create tables from ORM metadata.

    Used for fresh installs and tests; production deployments run Alembic migrations.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_schema(engine: AsyncEngine) -> None:
    """Drop all tables. Tests only."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
