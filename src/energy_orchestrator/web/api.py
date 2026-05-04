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
import json
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
from energy_orchestrator.solar import SolarCache
from energy_orchestrator.web.dependencies import (
    get_config,
    get_override_controller,
    get_price_cache,
    get_solar_cache,
    get_uow,
)
from energy_orchestrator.web.override import OverrideController

ConfigDep = Annotated[AppConfig, Depends(get_config)]
UowDep = Annotated[UnitOfWork, Depends(get_uow)]
OverrideDep = Annotated[OverrideController, Depends(get_override_controller)]
PriceCacheDep = Annotated[PriceCache, Depends(get_price_cache)]
SolarCacheDep = Annotated[SolarCache, Depends(get_solar_cache)]

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


@router.get("/solar")
async def get_solar(solar_cache: SolarCacheDep) -> dict[str, Any]:
    """Return cached Forecast.Solar output: hourly summed-watts time series for
    today + tomorrow plus per-plane breakdown and today's expected total kWh.

    The cache is populated by the orchestrator tick loop (every ~30 min).
    Until the first successful fetch this returns an empty payload with
    ``last_refresh: null``.
    """
    now = datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=2)
    forecast = solar_cache.forecast()
    if forecast is None:
        return {
            "last_refresh": None,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "watt_hours_today": None,
            "watt_hours_tomorrow": None,
            "points": [],
            "per_plane": {},
        }
    points = [
        {"timestamp": p.timestamp.isoformat(), "watts": p.watts}
        for p in forecast.points
        if start <= p.timestamp < end
    ]
    per_plane = {
        name: [
            {"timestamp": p.timestamp.isoformat(), "watts": p.watts}
            for p in series
            if start <= p.timestamp < end
        ]
        for name, series in forecast.per_plane.items()
    }
    return {
        "last_refresh": (
            solar_cache.last_refresh.isoformat() if solar_cache.last_refresh else None
        ),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "watt_hours_today": forecast.watt_hours_today,
        "watt_hours_tomorrow": forecast.watt_hours_tomorrow,
        "points": points,
        "per_plane": per_plane,
    }


@router.get("/logs/stream")
async def stream_logs(request: Request, config: ConfigDep) -> StreamingResponse:
    """Server-sent-event stream of the rotating JSON log file.

    Each SSE ``data:`` event carries one log line (already JSON). Client
    parses it and renders. On connect we replay only lines from the current
    server session (anything timestamped at or after ``app.state.session_started_at``),
    then follow new lines as they appear, reopening the file if the rotating
    handler swaps it out.
    """
    log_path = Path(config.logging.log_dir) / "energy_orchestrator.log"
    session_started_at: datetime = request.app.state.session_started_at
    return StreamingResponse(
        _tail_log_sse(request, log_path, session_started_at),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_POLL_INTERVAL_S = 0.4


def _line_in_session(line: str, session_started_at: datetime) -> bool:
    """True if a JSON log line's timestamp is at/after the session start.

    Non-JSON lines or lines without a parseable timestamp are kept (rare —
    they shouldn't appear in our structured log, and dropping them would hide
    surprises). Comparison happens in UTC.
    """
    try:
        rec = json.loads(line)
    except ValueError:
        return True
    ts_text = rec.get("timestamp")
    if not isinstance(ts_text, str):
        return True
    try:
        ts = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts >= session_started_at


async def _tail_log_sse(
    request: Request, log_path: Path, session_started_at: datetime
) -> AsyncIterator[str]:
    # Wait for the file to exist (first run before any logs have been written).
    while not log_path.exists():
        if await request.is_disconnected():
            return
        await asyncio.sleep(_POLL_INTERVAL_S)

    f = await asyncio.to_thread(open, log_path, "r", encoding="utf-8", errors="replace")
    try:
        # Replay from the start, skipping lines older than the current session.
        # Once we reach EOF we transition to live tailing; new lines necessarily
        # belong to this session, so the timestamp filter is a no-op from then on.
        while True:
            if await request.is_disconnected():
                return
            line = await asyncio.to_thread(f.readline)
            if line:
                if _line_in_session(line, session_started_at):
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
