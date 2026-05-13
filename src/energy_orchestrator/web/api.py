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
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
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
    PricePointRow,
    Reading,
    SolarForecastPointRow,
    SourceName,
    SourceStatus,
)
from energy_orchestrator.devices.errors import DeviceError
from energy_orchestrator.devices.etrel import EtrelInchClient
from energy_orchestrator.prices import PriceCache
from energy_orchestrator.solar import SolarCache
from energy_orchestrator.web.dependencies import (
    get_config,
    get_etrel_client,
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
EtrelClientDep = Annotated[EtrelInchClient | None, Depends(get_etrel_client)]

router = APIRouter(prefix="/api")

# How recently a successful read counts as "OK" before it degrades to STALE.
_OK_THRESHOLD = timedelta(minutes=5)
_DEGRADED_THRESHOLD = timedelta(minutes=30)


def _local_day_window(date_str: str) -> tuple[datetime, datetime]:
    """Parse ``YYYY-MM-DD`` as a server-local calendar day and return its
    UTC ``[start, end)`` instants.

    Browser and server are colocated on the home box, so the server's local
    TZ matches what the dashboard renders. ``astimezone()`` with no argument
    attaches the system tz; converting to UTC normalises to storage tz.
    """
    try:
        local_midnight = datetime.fromisoformat(f"{date_str}T00:00:00").astimezone()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid date: {date_str}") from exc
    start = local_midnight.astimezone(UTC)
    end = (local_midnight + timedelta(days=1)).astimezone(UTC)
    return start, end


# ----- request/response models ------------------------------------------------


class OverrideRequest(BaseModel):
    mode: OverrideMode
    minutes: int | None = Field(default=None, ge=1, le=1440)


class EtrelSetCurrentRequest(BaseModel):
    # Safety hard cap — 16 A is the wired-in ceiling for this installation.
    # Higher values are rejected at the API layer regardless of what the
    # charger's installer setting (custom_max_a) reports.
    amps: float = Field(..., ge=0.0, le=16.0)


# ----- serializers -------------------------------------------------------------


def _utc_aware(dt: datetime | None) -> datetime | None:
    """Re-attach UTC to a naive datetime — SQLite drops tzinfo on round-trip
    even with ``DateTime(timezone=True)``. All our timestamps are stored UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _iso_utc(dt: datetime | None) -> str | None:
    """ISO-8601 with explicit UTC offset, so JS ``new Date(...)`` parses it
    as UTC instead of local. SQLite drops tzinfo on round-trip, so we re-attach
    UTC before serializing — any naive datetime here is a UTC instant."""
    aware = _utc_aware(dt)
    return aware.isoformat() if aware else None


def _reading_to_dict(r: Reading | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return {
        "timestamp": _iso_utc(r.timestamp),
        "battery_soc_pct": r.battery_soc_pct,
        "battery_power_w": r.battery_power_w,
        "house_consumption_w": r.house_consumption_w,
        "production_w": r.production_w,
        "grid_feed_in_w": r.grid_feed_in_w,
        "car_charger_w": r.car_charger_w,
        "p1_active_power_w": r.p1_active_power_w,
        "small_solar_w": r.small_solar_w,
        "large_solar_w": r.large_solar_w,
        "etrel_power_w": r.etrel_power_w,
        "injection_price_eur_per_kwh": r.injection_price_eur_per_kwh,
        "consumption_price_eur_per_kwh": r.consumption_price_eur_per_kwh,
    }


def _decision_to_dict(d: Decision | None) -> dict[str, Any] | None:
    if d is None:
        return None
    return {
        "timestamp": _iso_utc(d.timestamp),
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
        "last_success_at": _iso_utc(s.last_success_at),
        "last_error_at": _iso_utc(s.last_error_at),
        "last_error_message": s.last_error_message,
        "last_payload": s.last_payload,
        "updated_at": _iso_utc(s.updated_at),
    }


def _override_to_dict(controller: OverrideController) -> dict[str, Any]:
    state = controller.get_active()
    if state is None:
        return {"mode": OverrideMode.AUTO.value, "expires_at": None}
    return {
        "mode": state.mode.value,
        "expires_at": _iso_utc(state.expires_at),
    }


def _classify_source_status(s: SourceStatus, now: datetime) -> str:
    """Return one of: OK / DEGRADED / ERROR / UNKNOWN.

    Whichever event (success or error) is more recent reflects the current
    state — a successful read after a recent error clears the ERROR badge,
    rather than holding it for a fixed cooldown.
    """
    last_success = _utc_aware(s.last_success_at)
    last_error = _utc_aware(s.last_error_at)
    if last_success is None and last_error is None:
        return "UNKNOWN"

    success_is_latest = last_success is not None and (
        last_error is None or last_success >= last_error
    )

    if success_is_latest:
        assert last_success is not None
        age = now - last_success
        if age <= _OK_THRESHOLD:
            return "OK"
        if age <= _DEGRADED_THRESHOLD:
            return "DEGRADED"
        return "ERROR"

    # Most recent event is an error.
    assert last_error is not None
    err_age = now - last_error
    if err_age <= _DEGRADED_THRESHOLD:
        return "ERROR"
    return "UNKNOWN"


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
    date: str | None = Query(default=None, description="YYYY-MM-DD (server-local day)"),
) -> dict[str, Any]:
    """Readings + decisions for either the last ``h`` hours or one specific
    server-local calendar day. ``date`` wins when both are supplied."""
    async with uow:
        if date is not None:
            start, end = _local_day_window(date)
            readings = list(await uow.readings.between(start, end))
            decisions = list(await uow.decisions.between(start, end))
        else:
            readings = list(await uow.readings.recent(hours=h))
            decisions = list(await uow.decisions.recent(hours=h))
    return {
        "hours": h,
        "date": date,
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
                "last_success_at": _iso_utc(row.last_success_at),
                "last_error_at": _iso_utc(row.last_error_at),
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
async def get_prices(
    price_cache: PriceCacheDep,
    uow: UowDep,
    date: str | None = Query(default=None, description="YYYY-MM-DD (server-local day)"),
) -> dict[str, Any]:
    """Return day-ahead price points.

    Without ``date``, returns the in-memory cache's current window
    (yesterday + today + tomorrow UTC) — fresh, refreshed by the tick loop.

    With ``date=YYYY-MM-DD``, queries the persisted ``price_points`` table
    for a window that covers the local calendar day plus one day on each
    side (chart clips client-side; the slop matches the cache window).
    """
    if date is not None:
        local_start, local_end = _local_day_window(date)
        # Widen the persisted-window read to mirror the cache's slop, so a
        # local-day chart stays fully populated near midnight regardless of
        # the server's UTC offset.
        start = local_start - timedelta(days=1)
        end = local_end + timedelta(days=1)
        async with uow:
            rows: Sequence[PricePointRow] = await uow.price_points.between(start, end)
        return {
            "last_refresh": None,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "date": date,
            "prices": [
                {
                    "timestamp": _iso_utc(r.timestamp),
                    "consumption_eur_per_kwh": r.consumption_eur_per_kwh,
                    "injection_eur_per_kwh": r.injection_eur_per_kwh,
                }
                for r in rows
            ],
        }

    now = datetime.now(UTC)
    today_utc_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_utc_midnight - timedelta(days=1)
    end = today_utc_midnight + timedelta(days=2)
    points = price_cache.points_in_range(start, end)
    return {
        "last_refresh": (
            price_cache.last_refresh.isoformat() if price_cache.last_refresh else None
        ),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "date": None,
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
async def get_solar(
    solar_cache: SolarCacheDep,
    uow: UowDep,
    date: str | None = Query(default=None, description="YYYY-MM-DD (server-local day)"),
) -> dict[str, Any]:
    """Return Forecast.Solar output.

    Without ``date``, serves the in-memory cache (today + tomorrow plus
    today/tomorrow kWh totals). With ``date=YYYY-MM-DD`` the persisted
    ``solar_forecast_points`` table is queried for that local calendar day;
    aggregate kWh fields are not available for historic days and come back
    as ``null``.
    """
    if date is not None:
        start, end = _local_day_window(date)
        async with uow:
            rows: Sequence[SolarForecastPointRow] = await uow.solar_forecast.between(start, end)
        # Re-derive the summed series and the per-plane breakdown from rows.
        # ``defaultdict``-style accumulation keeps a single pass.
        summed: dict[datetime, float] = {}
        per_plane: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            ts = _utc_aware(r.timestamp)
            assert ts is not None  # PK column is non-null
            summed[ts] = summed.get(ts, 0.0) + r.watts
            per_plane.setdefault(r.plane, []).append(
                {"timestamp": _iso_utc(r.timestamp), "watts": r.watts}
            )
        points = [
            {"timestamp": _iso_utc(ts), "watts": watts}
            for ts, watts in sorted(summed.items())
        ]
        return {
            "last_refresh": None,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "date": date,
            "watt_hours_today": None,
            "watt_hours_tomorrow": None,
            "points": points,
            "per_plane": per_plane,
        }

    now = datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=2)
    forecast = solar_cache.forecast()
    if forecast is None:
        return {
            "last_refresh": None,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "date": None,
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
        "date": None,
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


@router.post("/source-status/clear-errors")
async def clear_source_errors(uow: UowDep) -> dict[str, Any]:
    """Null out ``last_error_at`` + ``last_error_message`` on every source row.

    Used by the debug board's "Clear errors" button so stale failures stop
    cluttering the source-health table. The next failed tick will repopulate
    the columns; this is purely an acknowledge-and-clear action.
    """
    async with uow:
        rows = await uow.source_status.clear_all_errors()
        await uow.commit()
    return {"cleared": rows}


@router.post("/override")
async def post_override(body: OverrideRequest, controller: OverrideDep) -> dict[str, Any]:
    if body.mode is OverrideMode.AUTO and body.minutes is not None:
        raise HTTPException(
            status_code=400,
            detail="minutes must not be set when mode=auto",
        )
    controller.set(mode=body.mode, minutes=body.minutes)
    return _override_to_dict(controller)


@router.post("/shutdown")
async def post_shutdown() -> dict[str, Any]:
    """Stop the orchestrator and (on Linux) close the chromium kiosk.

    Triggered by the dashboard's Exit button. Kiosk fullscreen on the Pi
    leaves no other way to close the app — without this the user would
    need SSH or a keyboard shortcut to escape. Schedules the kill in a
    background task so the HTTP response can return before uvicorn exits.
    """
    pid = os.getpid()

    async def _shutdown() -> None:
        await asyncio.sleep(0.5)
        if sys.platform.startswith("linux"):
            try:
                subprocess.run(  # noqa: S603,S607
                    ["pkill", "-f", "chromium"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except FileNotFoundError:
                pass
        os.kill(pid, signal.SIGTERM)

    asyncio.create_task(_shutdown())
    return {"status": "shutting down"}


@router.post("/etrel/diagnostic-dump")
async def post_etrel_diagnostic_dump(
    config: ConfigDep, etrel_client: EtrelClientDep
) -> dict[str, Any]:
    """Re-run the Etrel register dump now (input+holding, regs 0..47 + 990..1039).

    Diagnostic for Sonnen Smart-E-Grid behavior: trigger this while the
    setpoint is clamped (``setpoint_diverged=true`` in the change-event
    log) and grep the resulting log entry for the float32 column matching
    the observed ``setpoint_a``. The matching register is where Sonnen's
    cluster channel writes its limit.
    """
    if config.etrel is None:
        raise HTTPException(status_code=400, detail="Etrel charger is not configured")
    if etrel_client is None:
        raise HTTPException(
            status_code=503,
            detail="Etrel client unavailable (tick loop not running)",
        )
    await etrel_client.force_diagnostic_dump()
    return {"status": "ok", "message": "diagnostic dump triggered, see log"}


@router.post("/etrel/set-current")
async def post_etrel_set_current(
    body: EtrelSetCurrentRequest,
    config: ConfigDep,
    etrel_client: EtrelClientDep,
) -> dict[str, Any]:
    """Manual write to the Etrel set-current setpoint (holding reg 8..9).

    Diagnostic endpoint — verifies that we can actually steer the charger
    rather than just observe it. Sonnen's Smart-E-Grid backend may overwrite
    the setpoint within seconds; this endpoint is for the "did our write
    take effect" test, not as a control loop.

    Routes through the **tick loop's** Etrel client, not a fresh instance —
    the firmware on this unit drops PDUs on a second concurrent Modbus TCP
    connection even when its handshake passes. The client's internal lock
    serializes the read tick against this write so they don't interleave.

    Always attempts a post-write readback of the same register, even when
    the write itself raised — Etrel firmware variants have been observed
    to apply a write while dropping the ACK, so the readback is the only
    reliable signal of whether the write took effect. Returns 200 with a
    structured body in all cases (write may have succeeded, failed, or
    succeeded silently); the caller inspects ``write_succeeded`` and
    ``set_current_a_after`` to decide.
    """
    if config.etrel is None:
        raise HTTPException(status_code=400, detail="Etrel charger is not configured")
    if etrel_client is None:
        raise HTTPException(
            status_code=503,
            detail="Etrel client unavailable (tick loop not running)",
        )
    write_error: str | None = None
    set_current_a_after: float | None = None
    readback_error: str | None = None
    try:
        await etrel_client.set_charging_current_a(body.amps)
    except DeviceError as e:
        write_error = str(e)
    try:
        set_current_a_after = await etrel_client.read_set_current_a()
    except DeviceError as e:
        readback_error = str(e)
    return {
        "amps_requested": body.amps,
        "write_succeeded": write_error is None,
        "write_error": write_error,
        "set_current_a_after": set_current_a_after,
        "readback_error": readback_error,
    }
