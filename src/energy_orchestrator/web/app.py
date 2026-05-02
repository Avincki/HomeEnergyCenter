"""FastAPI application factory + lifespan.

The lifespan opens the SQLite engine, ensures the schema exists, builds the
``PriceCache`` and ``OverrideController``, and (unless explicitly disabled)
starts the orchestrator tick loop. Tests pass ``start_tick_loop=False`` so
the test app doesn't try to talk to non-existent devices on every fixture.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from energy_orchestrator.config import AppConfig, load_config
from energy_orchestrator.data import (
    create_engine,
    create_session_factory,
    init_schema,
)
from energy_orchestrator.monitoring import configure_logging
from energy_orchestrator.orchestrator import TickLoop
from energy_orchestrator.prices import PriceCache
from energy_orchestrator.web.api import router as api_router
from energy_orchestrator.web.override import OverrideController
from energy_orchestrator.web.views import router as views_router

_THIS_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _THIS_DIR / "static"
_TEMPLATES_DIR = _THIS_DIR / "templates"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    config: AppConfig = app.state.config
    # Idempotent — main.py also calls this before uvicorn.run, but direct
    # create_app() callers (tests, gui-launched servers) need it too.
    configure_logging(config.logging)
    db_engine = create_engine(config.storage.sqlite_path)
    await init_schema(db_engine)
    session_factory = create_session_factory(db_engine)
    override_controller = OverrideController()
    price_cache = PriceCache()

    app.state.db_engine = db_engine
    app.state.session_factory = session_factory
    app.state.override_controller = override_controller
    app.state.price_cache = price_cache

    tick_loop: TickLoop | None = None
    if app.state.start_tick_loop:
        tick_loop = TickLoop(
            config=config,
            session_factory=session_factory,
            override_controller=override_controller,
            price_cache=price_cache,
        )
        await tick_loop.start()
    app.state.tick_loop = tick_loop

    try:
        yield
    finally:
        if tick_loop is not None:
            await tick_loop.stop()
        await db_engine.dispose()


def create_app(
    config: AppConfig | None = None,
    *,
    start_tick_loop: bool = True,
    config_path: str | Path | None = None,
) -> FastAPI:
    resolved_path: Path | None = None
    if config is None:
        env_path = config_path if config_path is not None else os.environ.get(
            "EO_CONFIG", "config.yaml"
        )
        resolved_path = Path(env_path).resolve()
        config = load_config(resolved_path)
    elif config_path is not None:
        resolved_path = Path(config_path).resolve()

    app = FastAPI(
        title="Energy Orchestrator",
        description=(
            "Self-hosted Belgian dynamic-tariff orchestrator. Reads sonnen/HomeWizard/"
            "SolarEdge, decides ON/OFF every poll_interval_s, and exposes a dashboard "
            "and JSON API for inspection and manual override."
        ),
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.state.config = config
    app.state.config_path = resolved_path
    app.state.start_tick_loop = start_tick_loop

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.include_router(api_router)
    app.include_router(views_router)

    return app


__all__ = ["create_app"]
