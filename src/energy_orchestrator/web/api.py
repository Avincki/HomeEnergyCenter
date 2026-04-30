"""JSON API endpoints. Read-only over the DB plus the override POST.

Endpoints (all under ``/api``):
  GET  /state                 latest reading + decision + sources
  GET  /history?h=24          readings + decisions, last N hours
  GET  /sources               last success/error per source
  GET  /health                config + per-source health snapshot
  GET  /prices                upcoming day-ahead prices (empty until tick loop wired)
  POST /override              { mode, minutes? } — apply or clear override
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
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
from energy_orchestrator.web.dependencies import (
    get_config,
    get_override_controller,
    get_uow,
)
from energy_orchestrator.web.override import OverrideController

ConfigDep = Annotated[AppConfig, Depends(get_config)]
UowDep = Annotated[UnitOfWork, Depends(get_uow)]
OverrideDep = Annotated[OverrideController, Depends(get_override_controller)]

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

    expected = [
        SourceName.SONNEN.value,
        SourceName.CAR_CHARGER.value,
        SourceName.P1_METER.value,
        SourceName.SMALL_SOLAR.value,
        SourceName.SOLAREDGE.value,
        SourceName.PRICES.value,
    ]
    sources_health = []
    overall_ok = True
    for name in expected:
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
async def get_prices() -> dict[str, Any]:
    # Populated by the orchestrator tick loop (next phase). Empty for now.
    return {
        "prices": [],
        "note": "price cache populates once the orchestrator tick loop is running",
    }


@router.post("/override")
async def post_override(body: OverrideRequest, controller: OverrideDep) -> dict[str, Any]:
    if body.mode is OverrideMode.AUTO and body.minutes is not None:
        raise HTTPException(
            status_code=400,
            detail="minutes must not be set when mode=auto",
        )
    controller.set(mode=body.mode, minutes=body.minutes)
    return _override_to_dict(controller)
