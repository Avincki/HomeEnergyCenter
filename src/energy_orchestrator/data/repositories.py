from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, TypeVar, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from energy_orchestrator.data.models import Base, Decision, Reading, SourceStatus

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """Common operations available to every repository."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, obj: ModelT) -> ModelT:
        self._session.add(obj)
        await self._session.flush()
        return obj


class ReadingsRepository(BaseRepository[Reading]):
    model = Reading

    async def recent(self, hours: float) -> Sequence[Reading]:
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        stmt = select(Reading).where(Reading.timestamp >= cutoff).order_by(Reading.timestamp)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def latest(self) -> Reading | None:
        stmt = select(Reading).order_by(Reading.timestamp.desc()).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def prune(self, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        stmt = delete(Reading).where(Reading.timestamp < cutoff)
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount or 0


class DecisionsRepository(BaseRepository[Decision]):
    model = Decision

    async def recent(self, hours: float) -> Sequence[Decision]:
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        stmt = select(Decision).where(Decision.timestamp >= cutoff).order_by(Decision.timestamp)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def latest(self) -> Decision | None:
        stmt = select(Decision).order_by(Decision.timestamp.desc()).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def prune(self, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        stmt = delete(Decision).where(Decision.timestamp < cutoff)
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount or 0


class SourceStatusRepository(BaseRepository[SourceStatus]):
    model = SourceStatus

    async def get(self, source_name: str) -> SourceStatus | None:
        return await self._session.get(SourceStatus, source_name)

    async def all(self) -> Sequence[SourceStatus]:
        result = await self._session.execute(select(SourceStatus))
        return result.scalars().all()

    async def record_success(
        self, source_name: str, payload: dict[str, Any] | None = None
    ) -> SourceStatus:
        now = datetime.now(UTC)
        status = await self.get(source_name)
        if status is None:
            status = SourceStatus(source_name=source_name)
            self._session.add(status)
        status.last_success_at = now
        if payload is not None:
            status.last_payload = payload
        status.updated_at = now
        await self._session.flush()
        return status

    async def record_error(self, source_name: str, message: str) -> SourceStatus:
        now = datetime.now(UTC)
        status = await self.get(source_name)
        if status is None:
            status = SourceStatus(source_name=source_name)
            self._session.add(status)
        status.last_error_at = now
        status.last_error_message = message
        status.updated_at = now
        await self._session.flush()
        return status
