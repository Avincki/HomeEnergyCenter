"""FastAPI application factory + lifespan.

For now the lifespan opens the SQLite engine and ensures the schema exists.
Devices, the price provider, and the background tick loop are wired in by
a follow-up phase — endpoints that depend on live readings simply return
empty results until then.
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
from energy_orchestrator.web.api import router as api_router
from energy_orchestrator.web.override import OverrideController
from energy_orchestrator.web.views import router as views_router

_THIS_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _THIS_DIR / "static"
_TEMPLATES_DIR = _THIS_DIR / "templates"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    config: AppConfig = app.state.config
    db_engine = create_engine(config.storage.sqlite_path)
    await init_schema(db_engine)
    app.state.db_engine = db_engine
    app.state.session_factory = create_session_factory(db_engine)
    app.state.override_controller = OverrideController()
    try:
        yield
    finally:
        await db_engine.dispose()


def create_app(config: AppConfig | None = None) -> FastAPI:
    if config is None:
        config_path = os.environ.get("EO_CONFIG", "config.yaml")
        config = load_config(config_path)

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

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.include_router(api_router)
    app.include_router(views_router)

    return app


__all__ = ["create_app"]
