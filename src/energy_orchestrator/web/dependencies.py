"""FastAPI dependency-injection helpers.

Each function pulls a dependency off ``app.state``, where the lifespan
handler stashed it during startup. Routes consume them via ``Depends(...)``.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from energy_orchestrator.config.models import AppConfig
from energy_orchestrator.data import UnitOfWork
from energy_orchestrator.web.override import OverrideController


def get_config(request: Request) -> AppConfig:
    return request.app.state.config  # type: ignore[no-any-return]


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory  # type: ignore[no-any-return]


def get_override_controller(request: Request) -> OverrideController:
    return request.app.state.override_controller  # type: ignore[no-any-return]


def get_uow(request: Request) -> UnitOfWork:
    """Build a fresh UoW per request. The route awaits ``async with`` itself."""
    return UnitOfWork(get_session_factory(request))
