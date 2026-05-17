"""FastAPI dependency-injection helpers.

Each function pulls a dependency off ``app.state``, where the lifespan
handler stashed it during startup. Routes consume them via ``Depends(...)``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from energy_orchestrator.config.models import AppConfig
from energy_orchestrator.data import UnitOfWork
from energy_orchestrator.devices.etrel import EtrelInchClient
from energy_orchestrator.prices import PriceCache
from energy_orchestrator.solar import SolarCache
from energy_orchestrator.web.override import OverrideController


def get_config(request: Request) -> AppConfig:
    return request.app.state.config  # type: ignore[no-any-return]


def get_config_path(request: Request) -> Path | None:
    return request.app.state.config_path  # type: ignore[no-any-return]


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory  # type: ignore[no-any-return]


def get_override_controller(request: Request) -> OverrideController:
    return request.app.state.override_controller  # type: ignore[no-any-return]


def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache  # type: ignore[no-any-return]


def get_solar_cache(request: Request) -> SolarCache:
    return request.app.state.solar_cache  # type: ignore[no-any-return]


def get_uow(request: Request) -> UnitOfWork:
    """Build a fresh UoW per request. The route awaits ``async with`` itself."""
    return UnitOfWork(get_session_factory(request))


def get_etrel_client(request: Request) -> EtrelInchClient | None:
    """Return the tick-loop-owned Etrel client, or ``None`` when unavailable.

    ``None`` covers two cases — Etrel isn't configured, or the tick loop
    wasn't started (tests). Routes should treat both as a 503-class error.
    """
    return getattr(request.app.state, "etrel_client", None)


def require_same_origin(request: Request) -> None:
    """CSRF guard: reject state-changing requests whose Origin doesn't match
    the request's own host.

    The web/API has no authentication — anyone on the tailnet can hit it,
    and so can a malicious page in another browser tab on the same machine
    (via a cross-site ``<form>`` POST). Modern browsers always set the
    ``Origin`` header on POST and refuse to let JavaScript forge it across
    origins, so requiring ``Origin == scheme://host`` rejects every
    cross-site submission without needing sessions or CSRF tokens.

    Same-origin caveats:
      * Curl / scripts must set ``Origin`` matching the Host header to call
        any POST. Documented in the API docs page.
      * If you ever put this behind a reverse proxy that rewrites Host,
        switch to checking against a configured allowed-origin list rather
        than ``request.url.scheme`` + Host header.
    """
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if not origin or not host:
        raise HTTPException(status_code=403, detail="missing Origin or Host header")
    expected = f"{request.url.scheme}://{host}"
    if origin != expected:
        raise HTTPException(status_code=403, detail="cross-origin request blocked")
