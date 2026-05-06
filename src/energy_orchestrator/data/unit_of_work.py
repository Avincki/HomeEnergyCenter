from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from energy_orchestrator.data.repositories import (
    DecisionsRepository,
    PricePointsRepository,
    ReadingsRepository,
    SolarForecastRepository,
    SourceStatusRepository,
)


class UnitOfWork(AbstractAsyncContextManager["UnitOfWork"]):
    """Wraps one AsyncSession and exposes the repositories.

    Use as `async with UnitOfWork(factory) as uow: ...; await uow.commit()`.
    Exiting the block without commit() rolls back. Exiting with an exception
    always rolls back.
    """

    readings: ReadingsRepository
    decisions: DecisionsRepository
    source_status: SourceStatusRepository
    price_points: PricePointsRepository
    solar_forecast: SolarForecastRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> UnitOfWork:
        self._session = self._session_factory()
        self.readings = ReadingsRepository(self._session)
        self.decisions = DecisionsRepository(self._session)
        self.source_status = SourceStatusRepository(self._session)
        self.price_points = PricePointsRepository(self._session)
        self.solar_forecast = SolarForecastRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._session is not None
        try:
            if exc is not None:
                await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork is not active — use it as an async context manager")
        return self._session

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
