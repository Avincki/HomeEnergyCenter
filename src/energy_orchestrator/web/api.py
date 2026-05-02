"""JSON API endpoints. Read-only over the DB plus the override POST.

Endpoints (all under ``/api``):
  GET  /state                 latest reading + decision + sources
  GET  /history?h=24          readings + decisions, last N hours
  GET  /sources               last success/error per source
  GET  /health                config + per-source health snapshot
  GET  /prices                today + tomorrow's day-ahead prices from the in-memory cache
  POST /override              { mode, minutes? } — apply or clear override
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from energy_orchestrator.config.models import AppConfig
from energy_orchestrator.data import UnitOfWork
from energy_orchestrator.data.models import (
    Decision,
    OverrideMode,
    Reading,
    SourceName,
    SourceStatus,
)
from energy_orchestrator.prices import PriceCache
from energy_orchestrator.web.dependencies import (
    get_config,
    get_override_controller,
    get_price_cache,
    get_uow,
)
from energy_orchestrator.web.override import OverrideController

ConfigDep = Annotated[AppConfig, Depends(get_config)]
UowDep = Annotated[UnitOfWork, Depends(get_uow)]
OverrideDep = Annotated[OverrideController, Depends(get_override_controller)]
PriceCacheDep = Annotated[PriceCache, Depends(get_price_cache)]

router = APIRouter(prefix="/api")

# How recently a successful read counts as "OK" before it degrades to STALE.
_OK_THRESHOLD = timedelta(minutes=5)
_DEGRADED_THRESHOLD = timedelta(minutes=30)


# ----- request/response models ------------------------------------------------


class OverrideRequest(BaseModel):
    mode: OverrideMode
    minutes: int | None = Field(default=None, ge=1, le=1440)


# ----- serializers -------------------------------------------------------------


def _reading_to_dict(r: Reading | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return {
        "timestamp": r.timestamp.isoformat(),
        "battery_soc_pct": r.battery_soc_pct,
        "battery_power_w": r.battery_power_w,
        "house_consumption_w": r.house_consumption_w,
        "production_w": r.production_w,
        "grid_feed_in_w": r.grid_feed_in_w,
        "car_charger_w": r.car_charger_w,
        "p1_active_power_w": r.p1_active_power_w,
        "small_solar_w": r.small_solar_w,
        "injection_price_eur_per_kwh": r.injection_price_eur_per_kwh,
        "consumption_price_eur_per_kwh": r.consumption_price_eur_per_kwh,
    }


def _decision_to_dict(d: Decision | None) -> dict[str, Any] | None:
    if d is None:
        return None
    return {
        "timestamp": d.timestamp.isoformat(),
        "state": d.state,
        "rule_fired": d.rule_fired,
        "reason": d.reason,
        "state_changed": d.state_changed,
        "manual_override": d.manual_override,
        "override_mode": d.override_mode,
        "forecast_end_soc_pct": d.forecast_end_soc_pct,
    }


def _source_to_dict(s: SourceStatus) -> dict[str, Any]:
    return {
        "source_name": s.source_name,
        "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
        "last_error_at": s.last_error_at.isoformat() if s.last_error_at else None,
        "last_error_message": s.last_error_message,
        "last_payload": s.last_payload,
        "updated_at": s.updated_at.isoformat(),
    }


def _override_to_dict(controller: OverrideController) -> dict[str, Any]:
    state = controller.get_active()
    if state is None:
        return {"mode": OverrideMode.AUTO.value, "expires_at": None}
    return {
        "mode": state.mode.value,
        "expires_at": state.expires_at.isoformat() if state.expires_at else None,
    }


def _utc_aware(dt: datetime | None) -> datetime | None:
    """Re-attach UTC to a naive datetime — SQLite drops tzinfo on round-trip
    even with ``DateTime(timezone=True)``. All our timestamps are stored UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _classify_source_status(s: SourceStatus, now: datetime) -> str:
    """Return one of: OK / DEGRADED / ERROR / UNKNOWN."""
    last_success = _utc_aware(s.last_success_at)
    last_error = _utc_aware(s.last_error_at)
    if last_success is None and last_error is None:
        return "UNKNOWN"
    last_success_age = (now - last_success) if last_success else None
    last_error_age = (now - last_error) if last_error else None
    if last_error_age is not None and last_error_age <= _OK_THRESHOLD:
        return "ERROR"
    if last_success_age is None or last_success_age > _DEGRADED_THRESHOLD:
        return "ERROR" if last_error_age is not None else "UNKNOWN"
    if last_success_age <= _OK_THRESHOLD:
        return "OK"
    return "DEGRADED"


# ----- routes ------------------------------------------------------------------


@router.get("/state")
async def get_state(
    uow: UowDep,
    controller: OverrideDep,
) -> dict[str, Any]:
    async with uow:
        latest_reading = await uow.readings.latest()
        latest_decision = await uow.decisions.latest()
        sources = list(await uow.source_status.all())
    return {
        "reading": _reading_to_dict(latest_reading),
        "decision": _decision_to_dict(latest_decision),
        "override": _override_to_dict(controller),
        "sources": [_source_to_dict(s) for s in sources],
    }


@router.get("/history")
async def get_history(
    uow: UowDep,
    h: int = Query(default=24, ge=1, le=24 * 30),
) -> dict[str, Any]:
    async with uow:
        readings = list(await uow.readings.recent(hours=h))
        decisions = list(await uow.decisions.recent(hours=h))
    return {
        "hours": h,
        "readings": [_reading_to_dict(r) for r in readings],
        "decisions": [_decision_to_dict(d) for d in decisions],
    }


@router.get("/sources")
async def get_sources(uow: UowDep) -> list[dict[str, Any]]:
    async with uow:
        return [_source_to_dict(s) for s in await uow.source_status.all()]


@router.get("/health")
async def get_health(config: ConfigDep, uow: UowDep) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with uow:
        rows = {s.source_name: s for s in await uow.source_status.all()}

    sources_health = []
    overall_ok = True
    for source in SourceName:
        name = source.value
        row = rows.get(name)
        if row is None:
            status = "UNKNOWN"
            sources_health.append(
                {"source_name": name, "status": status, "last_error_message": None}
            )
            overall_ok = False
            continue
        status = _classify_source_status(row, now)
        if status != "OK":
            overall_ok = False
        sources_health.append(
            {
                "source_name": name,
                "status": status,
                "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
                "last_error_at": row.last_error_at.isoformat() if row.last_error_at else None,
                "last_error_message": row.last_error_message,
            }
        )

    return {
        "status": "ok" if overall_ok else "degraded",
        "config_loaded": True,
        "dry_run": config.decision.dry_run,
        "sources": sources_health,
    }


@router.get("/prices")
async def get_prices(price_cache: PriceCacheDep) -> dict[str, Any]:
    """Return cached day-ahead price points covering today + tomorrow (UTC).

    The cache is populated by the orchestrator tick loop. Until the loop has
    produced a successful fetch, ``prices`` is an empty list and
    ``last_refresh`` is ``null``.
    """
    now = datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=2)
    points = price_cache.points_in_range(start, end)
    return {
        "last_refresh": (
            price_cache.last_refresh.isoformat() if price_cache.last_refresh else None
        ),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "prices": [
            {
                "timestamp": p.timestamp.isoformat(),
                "consumption_eur_per_kwh": p.consumption_eur_per_kwh,
                "injection_eur_per_kwh": p.injection_eur_per_kwh,
            }
            for p in points
        ],
    }


@router.get("/logs/stream")
async def stream_logs(request: Request, config: ConfigDep) -> StreamingResponse:
    """Server-sent-event stream of the rotating JSON log file.

    Each SSE ``data:`` event carries one log line (already JSON). Client
    parses it and renders. On startup we replay the tail of the file so the
    page has immediate context; thereafter we follow new lines as they
    appear, reopening the file if the rotating handler swaps it out.
    """
    log_path = Path(config.logging.log_dir) / "energy_orchestrator.log"
    return StreamingResponse(
        _tail_log_sse(request, log_path),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_INITIAL_TAIL_BYTES = 16 * 1024  # ~100 lines of context on connect
_POLL_INTERVAL_S = 0.4


async def _tail_log_sse(request: Request, log_path: Path) -> AsyncIterator[str]:
    # Wait for the file to exist (first run before any logs have been written).
    while not log_path.exists():
        if await request.is_disconnected():
            return
        await asyncio.sleep(_POLL_INTERVAL_S)

    f = await asyncio.to_thread(open, log_path, "r", encoding="utf-8", errors="replace")
    try:
        # Seek back ~16 KB so the user lands on recent context, not an empty page.
        await asyncio.to_thread(f.seek, 0, 2)
        end = await asyncio.to_thread(f.tell)
        start = max(0, end - _INITIAL_TAIL_BYTES)
        await asyncio.to_thread(f.seek, start)
        if start > 0:
            await asyncio.to_thread(f.readline)  # discard partial first line

        while True:
            if await request.is_disconnected():
                return
            line = await asyncio.to_thread(f.readline)
            if line:
                yield f"data: {line.rstrip()}\n\n"
                continue

            # No new data — detect rotation (file shrank or was replaced).
            try:
                current_size = log_path.stat().st_size
            except FileNotFoundError:
                current_size = 0
            if current_size < f.tell():
                await asyncio.to_thread(f.close)
                while not log_path.exists():
                    if await request.is_disconnected():
                        return
                    await asyncio.sleep(_POLL_INTERVAL_S)
                f = await asyncio.to_thread(
                    open, log_path, "r", encoding="utf-8", errors="replace"
                )
                continue

            await asyncio.sleep(_POLL_INTERVAL_S)
    finally:
        await asyncio.to_thread(f.close)


@router.post("/override")
async def post_override(body: OverrideRequest, controller: OverrideDep) -> dict[str, Any]:
    if body.mode is OverrideMode.AUTO and body.minutes is not None:
        raise HTTPException(
            status_code=400,
            detail="minutes must not be set when mode=auto",
        )
    controller.set(mode=body.mode, minutes=body.minutes)
    return _override_to_dict(controller)
