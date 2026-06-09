"""JSON API endpoints. Read-only over the DB plus the override POST.

Endpoints (all under ``/api``):
  GET  /state                 latest reading + decision + sources
  GET  /history?h=24          readings + decisions, last N hours
  GET  /sources               last success/error per source
  GET  /health                config + per-source health snapshot
  GET  /prices                today + tomorrow's day-ahead prices from the in-memory cache
  POST /override              { mode, minutes? } — apply or clear override
  POST /solaredge/test-toggle flip the inverter limit 0%/100% (manual hardware probe)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

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
from energy_orchestrator.decision.charger_control import ChargerMode
from energy_orchestrator.devices.errors import DeviceError
from energy_orchestrator.devices.etrel import EtrelInchClient
from energy_orchestrator.prices import PriceCache
from energy_orchestrator.solar import SolarCache
from energy_orchestrator.vehicle import VehicleCache, VehicleRecord
from energy_orchestrator.web.dependencies import (
    get_charger_status,
    get_config,
    get_etrel_client,
    get_override_controller,
    get_price_cache,
    get_solar_cache,
    get_uow,
    get_vehicle_cache,
    require_same_origin,
)
from energy_orchestrator.web.override import OverrideController

ConfigDep = Annotated[AppConfig, Depends(get_config)]
UowDep = Annotated[UnitOfWork, Depends(get_uow)]
OverrideDep = Annotated[OverrideController, Depends(get_override_controller)]
PriceCacheDep = Annotated[PriceCache, Depends(get_price_cache)]
SolarCacheDep = Annotated[SolarCache, Depends(get_solar_cache)]
EtrelClientDep = Annotated[EtrelInchClient | None, Depends(get_etrel_client)]
ChargerStatusDep = Annotated[dict[str, Any] | None, Depends(get_charger_status)]
VehicleCacheDep = Annotated[VehicleCache, Depends(get_vehicle_cache)]

router = APIRouter(prefix="/api")

# How recently a successful read counts as "OK" before it degrades to STALE.
_OK_THRESHOLD = timedelta(minutes=5)
_DEGRADED_THRESHOLD = timedelta(minutes=30)

# Strong references to fire-and-forget background tasks. asyncio keeps only a
# weak reference to a bare ``create_task`` result, so without this the task can
# be garbage-collected before it runs (RUF006); the done-callback drops it.
_background_tasks: set[asyncio.Task[Any]] = set()


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


class ChargerModeRequest(BaseModel):
    """Switch the runtime charger mode from the Etrel tile.

    ``amps`` is required for FORCED (the setpoint to hold) and ignored for
    OPTIMIZED. The 16 A ceiling is the same hard cap as the manual set-current.
    """

    mode: ChargerMode
    amps: float | None = Field(default=None, ge=0.0, le=16.0)

    @model_validator(mode="after")
    def _forced_needs_amps(self) -> ChargerModeRequest:
        if self.mode is ChargerMode.FORCED and self.amps is None:
            raise ValueError("amps is required when mode is 'forced'")
        return self


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
        "ev_soc_pct": r.ev_soc_pct,
        "injection_price_eur_per_kwh": r.injection_price_eur_per_kwh,
        "consumption_price_eur_per_kwh": r.consumption_price_eur_per_kwh,
    }


def _vehicle_to_dict(
    record: VehicleRecord | None, config: AppConfig, now: datetime
) -> dict[str, Any] | None:
    """Serialize the cached EV telemetry plus the derived trust signals.

    ``fresh`` (record recent enough) and ``at_home`` (within the configured
    geofence) are the gates a future charge-control rule would consult; surfaced
    here so the dashboard can show *why* a SoC is or isn't being trusted.
    Returns ``None`` when Tronity isn't configured or no record exists yet.
    """
    cfg = config.tronity
    if cfg is None or record is None:
        return None
    age = record.age(now)
    return {
        "soc_pct": record.soc_pct,
        "plugged": record.plugged,
        "charging": record.charging,
        "range_km": record.range_km,
        "odometer_km": record.odometer_km,
        "charger_power_kw": record.charger_power_kw,
        "latitude": record.latitude,
        "longitude": record.longitude,
        "recorded_at": _iso_utc(record.recorded_at),
        "fetched_at": _iso_utc(record.fetched_at),
        "age_s": age.total_seconds() if age is not None else None,
        "fresh": record.is_fresh(now, timedelta(seconds=cfg.stale_after_s)),
        "at_home": record.at_home(cfg.home_latitude, cfg.home_longitude, cfg.geofence_radius_m),
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
    charger: ChargerStatusDep,
    config: ConfigDep,
    vehicle_cache: VehicleCacheDep,
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
        "charger": charger,
        "vehicle": _vehicle_to_dict(vehicle_cache.record(), config, datetime.now(UTC)),
    }


@router.get("/vehicle")
async def get_vehicle(
    config: ConfigDep,
    vehicle_cache: VehicleCacheDep,
) -> dict[str, Any]:
    """Latest cached EV telemetry (Tronity) plus freshness/geofence signals.

    ``vehicle`` is ``None`` when Tronity isn't configured or no record has been
    fetched yet; ``last_refresh`` is when the cache was last written.
    """
    return {
        "vehicle": _vehicle_to_dict(vehicle_cache.record(), config, datetime.now(UTC)),
        "last_refresh": _iso_utc(vehicle_cache.last_refresh),
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
            {"timestamp": _iso_utc(ts), "watts": watts} for ts, watts in sorted(summed.items())
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
async def stream_logs(
    request: Request,
    config: ConfigDep,
    replay_hours: Annotated[float | None, Query(gt=0.0, le=720.0)] = None,
) -> StreamingResponse:
    """Server-sent-event stream of the rotating JSON log file.

    Each SSE ``data:`` event carries one log line (already JSON). Client
    parses it and renders, then follows new lines as they appear (reopening
    the file if the rotating handler swaps it out).

    The replay window on connect depends on ``replay_hours``:

    * **unset (default)** — replay only the current server session (lines at
      or after ``app.state.session_started_at``). This is what the ``/logs``
      page wants: a clean view of the running process.
    * **set** — replay at least the last ``replay_hours`` hours, spanning a
      restart if one happened inside that window. The debug "Rule decisions"
      panel uses this so a mid-run restart doesn't wipe the decision history.
      We take the *earlier* of (session start, now - replay_hours) so the
      window is never shorter than either the running session or the request.

    Note: the replay can only reach as far back as the *current* log file —
    if size-based rotation moved older lines to ``.log.1`` within the window,
    the replay starts at the current file's first line.
    """
    log_path = Path(config.logging.log_dir) / "energy_orchestrator.log"
    session_started_at: datetime = request.app.state.session_started_at
    if replay_hours is None:
        replay_since = session_started_at
    else:
        replay_since = min(
            session_started_at,
            datetime.now(UTC) - timedelta(hours=replay_hours),
        )
    return StreamingResponse(
        _tail_log_sse(request, log_path, replay_since),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_POLL_INTERVAL_S = 0.4


def _line_at_or_after(line: str, replay_since: datetime) -> bool:
    """True if a JSON log line's timestamp is at/after ``replay_since``.

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
    return ts >= replay_since


async def _tail_log_sse(
    request: Request, log_path: Path, replay_since: datetime
) -> AsyncIterator[str]:
    # Wait for the file to exist (first run before any logs have been written).
    while not await asyncio.to_thread(log_path.exists):
        if await request.is_disconnected():
            return
        await asyncio.sleep(_POLL_INTERVAL_S)

    f = await asyncio.to_thread(open, log_path, "r", encoding="utf-8", errors="replace")
    try:
        # Replay from the start, skipping lines older than the replay window.
        # Once we reach EOF we transition to live tailing; new lines are always
        # newer than the window, so the timestamp filter is a no-op from then on.
        while True:
            if await request.is_disconnected():
                return
            line = await asyncio.to_thread(f.readline)
            if line:
                if _line_at_or_after(line, replay_since):
                    yield f"data: {line.rstrip()}\n\n"
                continue

            # No new data — detect rotation (file shrank or was replaced).
            try:
                current_size = (await asyncio.to_thread(log_path.stat)).st_size
            except FileNotFoundError:
                current_size = 0
            if current_size < f.tell():
                await asyncio.to_thread(f.close)
                while not await asyncio.to_thread(log_path.exists):
                    if await request.is_disconnected():
                        return
                    await asyncio.sleep(_POLL_INTERVAL_S)
                f = await asyncio.to_thread(open, log_path, "r", encoding="utf-8", errors="replace")
                continue

            await asyncio.sleep(_POLL_INTERVAL_S)
    finally:
        await asyncio.to_thread(f.close)


@router.post("/source-status/clear-errors", dependencies=[Depends(require_same_origin)])
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


@router.post("/override", dependencies=[Depends(require_same_origin)])
async def post_override(body: OverrideRequest, controller: OverrideDep) -> dict[str, Any]:
    if body.mode is OverrideMode.AUTO and body.minutes is not None:
        raise HTTPException(
            status_code=400,
            detail="minutes must not be set when mode=auto",
        )
    controller.set(mode=body.mode, minutes=body.minutes)
    return _override_to_dict(controller)


@router.post("/shutdown", dependencies=[Depends(require_same_origin)])
async def post_shutdown() -> dict[str, Any]:
    """Close the chromium kiosk and drop back to the desktop session.

    Triggered by the dashboard's Exit button. Kiosk fullscreen on the Pi
    leaves no other way to close the browser — without this the user
    would need SSH or a keyboard shortcut to escape. The orchestrator
    service itself keeps running; only chromium is killed, so the
    underlying labwc/wayfire desktop comes back up.
    """

    async def _close_kiosk() -> None:
        await asyncio.sleep(0.5)
        if sys.platform.startswith("linux"):
            # Non-blocking subprocess so the event loop isn't stalled
            # (ASYNC221); suppress FileNotFoundError when pkill is absent.
            with contextlib.suppress(FileNotFoundError):
                proc = await asyncio.create_subprocess_exec(
                    "pkill",
                    "-f",
                    "chromium",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                await proc.wait()

    task = asyncio.create_task(_close_kiosk())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "closing kiosk"}


@router.post("/etrel/set-current", dependencies=[Depends(require_same_origin)])
async def post_etrel_set_current(
    request: Request,
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
    result = await _etrel_write_readback(etrel_client, body.amps)
    # If the rule-based controller is running it would overwrite this manual
    # setpoint on its next decision tick. Push the value into it so it adopts
    # the manual target instead of fighting it. Only when the write actually
    # took (ACKed, or readback confirms a silently-applied write).
    controller_target_set = False
    tick_loop = getattr(request.app.state, "tick_loop", None)
    if tick_loop is not None and result.pop("took"):
        controller_target_set = tick_loop.adopt_manual_charger_target(body.amps)
    return {**result, "controller_target_set": controller_target_set}


async def _etrel_write_readback(client: EtrelInchClient, amps: float) -> dict[str, Any]:
    """Write the set-current setpoint then read it back, never raising.

    Etrel firmware variants apply a write while dropping the ACK, so the
    readback — attempted even when the write raised — is the only reliable
    signal of whether it took. Returns the structured outcome plus ``took``
    (ACKed, or the readback confirms a silently-applied write) for callers that
    need to decide follow-up actions.
    """
    write_error: str | None = None
    set_current_a_after: float | None = None
    readback_error: str | None = None
    try:
        await client.set_charging_current_a(amps)
    except DeviceError as e:
        write_error = str(e)
    try:
        set_current_a_after = await client.read_set_current_a()
    except DeviceError as e:
        readback_error = str(e)
    took = write_error is None or (
        set_current_a_after is not None and abs(set_current_a_after - amps) < 0.1
    )
    return {
        "amps_requested": amps,
        "write_succeeded": write_error is None,
        "write_error": write_error,
        "set_current_a_after": set_current_a_after,
        "readback_error": readback_error,
        "took": took,
    }


@router.post("/solaredge/test-toggle", dependencies=[Depends(require_same_origin)])
async def post_solaredge_test_toggle(request: Request) -> dict[str, Any]:
    """Manually flip the SolarEdge inverter between 0 % and 100 % (operator probe).

    Writes the active-power-limit register DIRECTLY, bypassing the decision
    engine and ``decision.dry_run``, then reads it back — a "does the inverter
    actually obey curtailment" test, not a control loop. When ``dry_run`` is
    false the tick loop re-asserts the engine's decision on its next decision
    tick, so the flip isn't sticky. Returns 200 with a structured body in all
    cases; the caller inspects ``write_succeeded`` / ``took`` /
    ``active_power_limit_pct_after`` to tell "unreachable" from "accepted but
    ignored".
    """
    tick_loop = getattr(request.app.state, "tick_loop", None)
    if tick_loop is None:
        raise HTTPException(
            status_code=503,
            detail="SolarEdge control unavailable (tick loop not running)",
        )
    return await tick_loop.toggle_solaredge_limit_manual()  # type: ignore[no-any-return]


@router.post("/charger/mode", dependencies=[Depends(require_same_origin)])
async def post_charger_mode(
    request: Request,
    body: ChargerModeRequest,
    etrel_client: EtrelClientDep,
) -> dict[str, Any]:
    """Switch the runtime charger mode (Etrel tile Force / Optimized buttons).

    FORCED records the operator setpoint as sticky (held regardless of solar,
    daytime, or battery SoC; capped at 16 A) and does an immediate write +
    readback so the car starts now rather than on the next tick. OPTIMIZED hands
    control back to the rule engine. Requires charger control to be enabled
    (``charger_control.enabled``); otherwise there is no controller for the mode
    to act on and we return 409.
    """
    tick_loop = getattr(request.app.state, "tick_loop", None)
    if tick_loop is None:
        raise HTTPException(
            status_code=503, detail="Charger control unavailable (tick loop not running)"
        )
    state = tick_loop.set_charger_mode(body.mode, body.amps)
    if not state["active"]:
        raise HTTPException(
            status_code=409,
            detail="Charger control is disabled — enable it to use Optimized/Forced modes",
        )
    response: dict[str, Any] = {"mode": state["mode"], "forced_amps": state["forced_amps"]}
    # Immediate write for instant feedback when forcing; OPTIMIZED lets the rule
    # engine actuate on its next tick.
    if body.mode is ChargerMode.FORCED and etrel_client is not None:
        write = await _etrel_write_readback(etrel_client, state["forced_amps"])
        write.pop("took", None)
        response.update(write)
    return response
