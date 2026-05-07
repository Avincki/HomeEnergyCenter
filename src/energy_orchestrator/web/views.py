"""HTML view routes — dashboard at ``/``, debug at ``/debug``, config at ``/config``."""

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
from energy_orchestrator.gui.binding import (
    AppConfigForm,
    config_to_form,
    form_to_config,
    save_with_backup,
)
from energy_orchestrator.solar import SolarCache
from energy_orchestrator.web.api import _classify_source_status
from energy_orchestrator.web.config_form import SECTIONS
from energy_orchestrator.web.dependencies import (
    get_config,
    get_config_path,
    get_override_controller,
    get_solar_cache,
    get_uow,
)
from energy_orchestrator.web.override import OverrideController

ConfigDep = Annotated[AppConfig, Depends(get_config)]
ConfigPathDep = Annotated[Path | None, Depends(get_config_path)]
UowDep = Annotated[UnitOfWork, Depends(get_uow)]
OverrideDep = Annotated[OverrideController, Depends(get_override_controller)]
SolarCacheDep = Annotated[SolarCache, Depends(get_solar_cache)]

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_templates = Jinja2Templates(directory=_TEMPLATES_DIR)


def _localtime(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Render a stored UTC datetime in the server's local timezone.

    SQLite drops tzinfo on round-trip, so naive values arrive here — treat
    them as UTC. ``astimezone()`` with no argument converts to system-local.
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().strftime(fmt)


_templates.env.filters["localtime"] = _localtime

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
    solar_cache: SolarCacheDep,
) -> HTMLResponse:
    async with uow:
        latest_reading = await uow.readings.latest()
        latest_decision = await uow.decisions.latest()
        recent_decisions = list(await uow.decisions.recent(hours=24))
    forecast = solar_cache.forecast()
    solar_today_kwh = (
        forecast.watt_hours_today / 1000.0
        if forecast is not None and forecast.watt_hours_today is not None
        else None
    )
    return _templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "reading": latest_reading,
            "decision": latest_decision,
            "recent_decisions": recent_decisions[-20:],
            "override": _override_summary(controller),
            "solar_today_kwh": solar_today_kwh,
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


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request) -> HTMLResponse:
    return _templates.TemplateResponse(request=request, name="logs.html", context={})


@router.get("/docs", response_class=HTMLResponse)
async def api_docs(request: Request) -> HTMLResponse:
    return _templates.TemplateResponse(request=request, name="api.html", context={})


@router.get("/config", response_class=HTMLResponse)
async def config_form(
    request: Request,
    config: ConfigDep,
    config_path: ConfigPathDep,
) -> HTMLResponse:
    return _render_config_form(
        request=request,
        form=config_to_form(config),
        errors={},
        message=None,
        message_kind=None,
        config_path=config_path,
    )


@router.post("/config", response_class=HTMLResponse)
async def config_save(
    request: Request,
    config: ConfigDep,
    config_path: ConfigPathDep,
) -> HTMLResponse:
    raw = await request.form()
    form = _form_from_post(raw)

    new_config, errors = form_to_config(form, baseline=config)
    if errors or new_config is None:
        return _render_config_form(
            request=request,
            form=form,
            errors=errors,
            message=f"{len(errors)} validation error(s) — see red text below each field.",
            message_kind="error",
            config_path=config_path,
        )

    if config_path is None:
        return _render_config_form(
            request=request,
            form=form,
            errors={},
            message=(
                "Config validates, but no file path is bound to this app instance — "
                "save skipped. (Run via main.py / EO_CONFIG to enable saving.)"
            ),
            message_kind="error",
            config_path=config_path,
        )

    try:
        save_with_backup(new_config, config_path)
    except OSError as exc:
        return _render_config_form(
            request=request,
            form=form,
            errors={},
            message=f"Save failed: {exc}",
            message_kind="error",
            config_path=config_path,
        )

    return _render_config_form(
        request=request,
        form=config_to_form(new_config),
        errors={},
        message=(
            f"Saved to {config_path} (previous version kept as .bak). "
            "Restart the orchestrator for changes to take effect."
        ),
        message_kind="success",
        config_path=config_path,
    )


def _form_from_post(raw: Any) -> AppConfigForm:
    """Convert FastAPI form-data into the dotted-key form dict.

    Unchecked checkboxes don't appear in form data; we backfill them as
    "false" so the boolean fields validate cleanly.
    """
    out: AppConfigForm = {}
    for section in SECTIONS:
        for sub in section[1]:
            for f in sub.fields:
                if f.kind == "checkbox":
                    out[f.key] = "true" if raw.get(f.key) else "false"
                else:
                    value = raw.get(f.key)
                    out[f.key] = "" if value is None else str(value)
    return out


def _render_config_form(
    *,
    request: Request,
    form: AppConfigForm,
    errors: dict[str, str],
    message: str | None,
    message_kind: str | None,
    config_path: Path | None,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request=request,
        name="config.html",
        context={
            "sections": SECTIONS,
            "form": form,
            "errors": errors,
            "message": message,
            "message_kind": message_kind,
            "config_path": str(config_path) if config_path else None,
        },
    )


def _config_view(config: AppConfig) -> dict[str, Any]:
    """Sanitised, read-only config snapshot for the debug board.

    Secrets are scrubbed (Pydantic SecretStr already shows ``**********``,
    but we re-confirm here so a future config change can't leak by accident).
    """
    return {
        "poll_interval_s": config.poll_interval_s,
        "decision_interval_s": config.decision_interval_s,
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
            "large_solar": (
                None
                if config.homewizard.large_solar is None
                else {
                    "host": config.homewizard.large_solar.host,
                    "peak_w": config.homewizard.large_solar.peak_w,
                }
            ),
        },
        "solaredge": {
            "host": config.solaredge.host,
            "modbus_port": config.solaredge.modbus_port,
            "unit_id": config.solaredge.unit_id,
        },
        "etrel": (
            None
            if config.etrel is None
            else {
                "host": config.etrel.host,
                "modbus_port": config.etrel.modbus_port,
                "unit_id": config.etrel.unit_id,
            }
        ),
        "prices": {
            "provider": config.prices.provider.value,
            "area": config.prices.area,
            "api_key": "***" if config.prices.api_key else None,
            "injection_factor": config.prices.injection_factor,
            "injection_offset": config.prices.injection_offset,
        },
        "solar": (
            None
            if config.solar is None
            else {
                "latitude": config.solar.latitude,
                "longitude": config.solar.longitude,
                "api_key": "***" if config.solar.api_key else None,
                "damping_morning": config.solar.damping_morning,
                "damping_evening": config.solar.damping_evening,
                "planes": [
                    {
                        "name": p.name,
                        "declination": p.declination,
                        "azimuth": p.azimuth,
                        "kwp": p.kwp,
                    }
                    for p in config.solar.planes
                ],
            }
        ),
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
