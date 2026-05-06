from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, TypeVar, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from energy_orchestrator.data.models import (
    Base,
    Decision,
    PricePointRow,
    Reading,
    SolarForecastPointRow,
    SourceStatus,
)

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

    async def between(self, start: datetime, end: datetime) -> Sequence[Reading]:
        stmt = (
            select(Reading)
            .where(Reading.timestamp >= start, Reading.timestamp < end)
            .order_by(Reading.timestamp)
        )
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

    async def between(self, start: datetime, end: datetime) -> Sequence[Decision]:
        stmt = (
            select(Decision)
            .where(Decision.timestamp >= start, Decision.timestamp < end)
            .order_by(Decision.timestamp)
        )
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


class PricePointsRepository(BaseRepository[PricePointRow]):
    """Persistent store of day-ahead prices, written by the tick loop and
    read by the dashboard for historic days the in-memory cache no longer
    covers. Upsert keyed on ``timestamp`` so each provider refresh refreshes
    the same future rows in place."""

    model = PricePointRow

    async def upsert_many(
        self,
        rows: Iterable[tuple[datetime, float | None, float | None]],
    ) -> int:
        """``rows`` = ``(timestamp, consumption, injection)`` tuples. Returns
        the number of rows touched."""
        payload = [
            {
                "timestamp": ts,
                "consumption_eur_per_kwh": consumption,
                "injection_eur_per_kwh": injection,
            }
            for ts, consumption, injection in rows
        ]
        if not payload:
            return 0
        stmt = sqlite_insert(PricePointRow).values(payload)
        stmt = stmt.on_conflict_do_update(
            index_elements=[PricePointRow.timestamp],
            set_={
                "consumption_eur_per_kwh": stmt.excluded.consumption_eur_per_kwh,
                "injection_eur_per_kwh": stmt.excluded.injection_eur_per_kwh,
            },
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount or 0

    async def between(self, start: datetime, end: datetime) -> Sequence[PricePointRow]:
        stmt = (
            select(PricePointRow)
            .where(PricePointRow.timestamp >= start, PricePointRow.timestamp < end)
            .order_by(PricePointRow.timestamp)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def prune(self, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        stmt = delete(PricePointRow).where(PricePointRow.timestamp < cutoff)
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount or 0


class SolarForecastRepository(BaseRepository[SolarForecastPointRow]):
    """Persistent per-plane forecast.solar points. The summed series is
    derived on read by summing rows that share a timestamp; that lets the
    chart pick up new planes without a schema change."""

    model = SolarForecastPointRow

    async def upsert_per_plane(
        self,
        per_plane: Mapping[str, Sequence[tuple[datetime, float]]],
    ) -> int:
        """``per_plane`` = ``{plane_name: [(ts, watts), ...], ...}``. Returns
        the number of rows touched across all planes."""
        payload = [
            {"timestamp": ts, "plane": plane, "watts": watts}
            for plane, points in per_plane.items()
            for ts, watts in points
        ]
        if not payload:
            return 0
        stmt = sqlite_insert(SolarForecastPointRow).values(payload)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                SolarForecastPointRow.timestamp,
                SolarForecastPointRow.plane,
            ],
            set_={"watts": stmt.excluded.watts},
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount or 0

    async def between(
        self, start: datetime, end: datetime
    ) -> Sequence[SolarForecastPointRow]:
        stmt = (
            select(SolarForecastPointRow)
            .where(
                SolarForecastPointRow.timestamp >= start,
                SolarForecastPointRow.timestamp < end,
            )
            .order_by(SolarForecastPointRow.timestamp, SolarForecastPointRow.plane)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def prune(self, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        stmt = delete(SolarForecastPointRow).where(SolarForecastPointRow.timestamp < cutoff)
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

    async def clear_all_errors(self) -> int:
        """Null out ``last_error_at`` and ``last_error_message`` for every
        source. Used by the debug board's "Clear errors" action so old
        failures (already-recovered) stop displaying alongside the current
        state. Returns the number of rows affected."""
        stmt = update(SourceStatus).values(
            last_error_at=None,
            last_error_message=None,
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        await self._session.flush()
        return result.rowcount or 0
