"""HTML view routes — the dashboard at ``/`` and the debug board at ``/debug``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from energy_orchestrator.config.models import AppConfig
from energy_orchestrator.data import UnitOfWork
from energy_orchestrator.data.models import OverrideMode, SourceName
from energy_orchestrator.web.api import _classify_source_status
from energy_orchestrator.web.dependencies import (
    get_config,
    get_override_controller,
    get_uow,
)
from energy_orchestrator.web.override import OverrideController

ConfigDep = Annotated[AppConfig, Depends(get_config)]
UowDep = Annotated[UnitOfWork, Depends(get_uow)]
OverrideDep = Annotated[OverrideController, Depends(get_override_controller)]

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter()


def _override_summary(controller: OverrideController) -> dict[str, Any]:
    state = controller.get_active()
    if state is None:
        return {"active": False, "mode": "auto", "expires_at": None}
    return {
        "active": True,
        "mode": state.mode.value,
        "expires_at": state.expires_at,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    uow: UowDep,
    controller: OverrideDep,
) -> HTMLResponse:
    async with uow:
        latest_reading = await uow.readings.latest()
        latest_decision = await uow.decisions.latest()
        recent_decisions = list(await uow.decisions.recent(hours=24))
    return _templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "reading": latest_reading,
            "decision": latest_decision,
            "recent_decisions": recent_decisions[-20:],
            "override": _override_summary(controller),
        },
    )


@router.get("/debug", response_class=HTMLResponse)
async def debug_board(
    request: Request,
    config: ConfigDep,
    uow: UowDep,
    controller: OverrideDep,
) -> HTMLResponse:
    now = datetime.now(UTC)
    async with uow:
        sources_by_name = {s.source_name: s for s in await uow.source_status.all()}
        recent_decisions = list(await uow.decisions.recent(hours=24))

    health_rows = []
    for source in SourceName:
        name = source.value
        row = sources_by_name.get(name)
        health_rows.append(
            {
                "source_name": name,
                "row": row,
                "status": _classify_source_status(row, now) if row is not None else "UNKNOWN",
            }
        )

    return _templates.TemplateResponse(
        request=request,
        name="debug.html",
        context={
            "health_rows": health_rows,
            "recent_decisions": recent_decisions[-50:],
            "override": _override_summary(controller),
            "config_view": _config_view(config),
            "override_modes": [m.value for m in OverrideMode],
        },
    )


def _config_view(config: AppConfig) -> dict[str, Any]:
    """Sanitised, read-only config snapshot for the debug board.

    Secrets are scrubbed (Pydantic SecretStr already shows ``**********``,
    but we re-confirm here so a future config change can't leak by accident).
    """
    return {
        "poll_interval_s": config.poll_interval_s,
        "sonnen": {
            "host": config.sonnen.host,
            "port": config.sonnen.port,
            "api_version": config.sonnen.api_version.value,
            "auth_token": "***" if config.sonnen.auth_token else None,
            "capacity_kwh": config.sonnen.capacity_kwh,
        },
        "homewizard": {
            "car_charger": {
                "host": config.homewizard.car_charger.host,
                "charging_threshold_w": config.homewizard.car_charger.charging_threshold_w,
            },
            "p1_meter": {"host": config.homewizard.p1_meter.host},
            "small_solar": {
                "host": config.homewizard.small_solar.host,
                "peak_w": config.homewizard.small_solar.peak_w,
            },
        },
        "solaredge": {
            "host": config.solaredge.host,
            "modbus_port": config.solaredge.modbus_port,
            "unit_id": config.solaredge.unit_id,
        },
        "prices": {
            "provider": config.prices.provider.value,
            "area": config.prices.area,
            "api_key": "***" if config.prices.api_key else None,
            "injection_factor": config.prices.injection_factor,
            "injection_offset": config.prices.injection_offset,
        },
        "decision": {
            "battery_low_soc_pct": config.decision.battery_low_soc_pct,
            "battery_full_soc_pct": config.decision.battery_full_soc_pct,
            "hysteresis_pct": config.decision.hysteresis_pct,
            "forecast_horizon_h": config.decision.forecast_horizon_h,
            "dry_run": config.decision.dry_run,
        },
        "storage": {
            "sqlite_path": str(config.storage.sqlite_path),
            "history_retention_days": config.storage.history_retention_days,
        },
        "logging": {
            "log_dir": str(config.logging.log_dir),
            "level": config.logging.level,
            "retention_days": config.logging.retention_days,
        },
        "web": {"host": config.web.host, "port": config.web.port},
    }
