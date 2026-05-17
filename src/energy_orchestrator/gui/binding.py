"""Pure form-binding layer for the config editor — no tkinter imports.

The GUI maintains form state as a flat dict of dotted-path strings (e.g.
``"sonnen.host" -> "192.168.1.50"``). This module provides the conversions
in both directions plus an atomic YAML save that keeps a single ``.bak``
of the previous file.

Keeping everything here pure means we can unit-test the form logic without
ever instantiating a ``tk.Tk()`` — useful in CI and on headless runners.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr, ValidationError

from energy_orchestrator.config.models import AppConfig

# Form field type: dotted path -> string (everything is string in tkinter
# entry widgets; we coerce numeric values via Pydantic on validation).
AppConfigForm = dict[str, str]

# Field-keyed validation errors: dotted path -> message.
FormErrors = dict[str, str]


# Fields that should be written as a Path-rendered string in YAML, not a
# dict-of-pieces. Matches Pydantic ``Path`` fields in the model.
_PATH_FIELDS: frozenset[str] = frozenset(
    {
        "prices.csv_path",
        "storage.sqlite_path",
        "logging.log_dir",
    }
)

# Fields whose values are SecretStr in the model. Form values are plain
# strings; we wrap/unwrap at the boundary.
_SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "sonnen.auth_token",
        "prices.api_key",
    }
)

# Optional plain-string fields where an empty form input means "use the default"
# (i.e. None) — without this, Pydantic min_length validators on the model would
# reject the empty string instead of falling back.
_OPTIONAL_STRING_FIELDS: frozenset[str] = frozenset(
    {
        "prices.base_url",
    }
)


def config_to_form(config: AppConfig) -> AppConfigForm:
    """Flatten an :class:`AppConfig` into a dotted-key form dict.

    SecretStr values are unwrapped; ``None`` becomes the empty string;
    enums use their ``.value``; paths are rendered as POSIX strings.
    """
    nested = config.model_dump(mode="python")
    # Drop optional top-level sections that the web form doesn't render —
    # ``solar`` is always YAML-only. Otherwise _flatten would emit it as
    # ``solar: ""`` and form_to_config would feed that back to Pydantic,
    # which rejects empty-string-as-Config.
    nested.pop("solar", None)
    # Same treatment for optional nested sub-sections — a None value would
    # become a stray ``homewizard.large_solar: ""`` form key that Pydantic
    # would later reject as not-a-LargeSolarConfig.
    hw = nested.get("homewizard")
    if isinstance(hw, dict) and hw.get("large_solar") is None:
        hw.pop("large_solar", None)
    # Optional top-level ``etrel`` section: when None, populate the form with
    # blank fields rather than dropping it entirely so the user can fill in
    # an address to enable the device. Without this, the section would never
    # render any inputs the first time a config without ``etrel`` is loaded.
    if nested.get("etrel") is None:
        nested["etrel"] = {
            "host": "",
            "modbus_port": "502",
            "unit_id": "1",
            "timeout_s": "5.0",
            "retry_count": "3",
        }
    flat: AppConfigForm = {}
    _flatten(nested, prefix=(), out=flat)
    # model_dump leaves SecretStr as objects — unwrap.
    for key in _SECRET_FIELDS:
        secret = _walk(nested, key.split("."))
        flat[key] = "" if secret is None else _coerce_secret_to_str(secret)
    return flat


def form_to_config(
    form: AppConfigForm, *, baseline: AppConfig | None = None
) -> tuple[AppConfig | None, FormErrors]:
    """Build an :class:`AppConfig` from a flat form dict.

    Returns ``(config, {})`` on success, or ``(None, errors)`` mapping each
    failing dotted path to its Pydantic error message. Empty strings on
    optional fields (auth_token, api_key, csv_path) become ``None`` so
    Pydantic's ``Optional`` defaults take effect.

    Sections not represented in the form (``solar``) are carried over from
    ``baseline`` so a web-form save doesn't silently drop YAML-only config.
    """
    nested: dict[str, Any] = {}
    for key, raw_value in form.items():
        value: Any = raw_value
        if value == "" and (
            key in _SECRET_FIELDS
            or key in _PATH_FIELDS
            or key in _OPTIONAL_STRING_FIELDS
        ):
            value = None
        _set_nested(nested, key.split("."), value)

    # Optional sub-section: drop the whole large_solar subsection if the user
    # left ``host`` blank (or the placeholder slipped through from a stale
    # form serializer as a non-dict). Otherwise Pydantic would try to
    # validate empty strings against the required fields and reject them.
    hw = nested.get("homewizard")
    if isinstance(hw, dict):
        ls = hw.get("large_solar")
        if not isinstance(ls, dict):
            hw["large_solar"] = None
        elif not str(ls.get("host", "")).strip():
            hw["large_solar"] = None

    # Same treatment for the optional top-level ``etrel`` section: blank host
    # means "disabled". Set to None so AppConfig.etrel falls back to its
    # default rather than failing host validation on the empty string.
    et = nested.get("etrel")
    if not isinstance(et, dict):
        nested["etrel"] = None
    elif not str(et.get("host", "")).strip():
        nested["etrel"] = None

    if baseline is not None and baseline.solar is not None and "solar" not in nested:
        # Preserve the YAML-only solar section through web-form saves.
        nested["solar"] = {
            "latitude": baseline.solar.latitude,
            "longitude": baseline.solar.longitude,
            "api_key": (
                baseline.solar.api_key.get_secret_value()
                if baseline.solar.api_key is not None
                else None
            ),
            "damping_morning": baseline.solar.damping_morning,
            "damping_evening": baseline.solar.damping_evening,
            "calibration_factor": baseline.solar.calibration_factor,
            "planes": [
                {
                    "name": p.name,
                    "declination": p.declination,
                    "azimuth": p.azimuth,
                    "kwp": p.kwp,
                }
                for p in baseline.solar.planes
            ],
        }

    try:
        config = AppConfig.model_validate(nested)
    except ValidationError as exc:
        return None, _validation_errors_by_field(exc)
    return config, {}


def dump_yaml(config: AppConfig) -> str:
    """Render an :class:`AppConfig` to a YAML string.

    SecretStr values are serialised as their plaintext form (so the YAML
    file is the editable canonical source). Enums become their ``.value``.
    ``None`` values are emitted as YAML ``null``.
    """
    plain = _config_to_plain_dict(config)
    return yaml.safe_dump(plain, sort_keys=False, default_flow_style=False)


def save_with_backup(config: AppConfig, path: str | Path) -> None:
    """Write ``config`` to ``path`` atomically, keeping a single ``.bak``.

    The previous file (if any) is copied to ``<path>.bak`` before the new
    content is dropped in via ``os.replace``. Aborts with the original file
    intact if the temp write fails.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(dump_yaml(config), encoding="utf-8")
    if target.exists():
        shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
    os.replace(tmp, target)


# ----- internals --------------------------------------------------------------


def _flatten(obj: Any, prefix: tuple[str, ...], out: AppConfigForm) -> None:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            _flatten(v, (*prefix, str(k)), out)
        return
    key = ".".join(prefix)
    if key in _SECRET_FIELDS:
        # Filled in afterwards from the original AppConfig.
        return
    if obj is None:
        out[key] = ""
        return
    if isinstance(obj, Path):
        out[key] = obj.as_posix()
        return
    out[key] = str(obj)


def _walk(nested: Mapping[str, Any], parts: list[str]) -> Any:
    cur: Any = nested
    for p in parts:
        if not isinstance(cur, Mapping) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _set_nested(nested: dict[str, Any], parts: list[str], value: Any) -> None:
    cur = nested
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _coerce_secret_to_str(value: Any) -> str:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if value is None:
        return ""
    return str(value)


def _validation_errors_by_field(exc: ValidationError) -> FormErrors:
    out: FormErrors = {}
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        # Pydantic emits the most specific error first; keep that one.
        out.setdefault(loc, err["msg"])
    return out


def _config_to_plain_dict(config: AppConfig) -> dict[str, Any]:
    """Walk AppConfig manually so SecretStr/Path/Enum get correct shapes."""
    out: dict[str, Any] = {
        "poll_interval_s": config.poll_interval_s,
        "decision_interval_s": config.decision_interval_s,
        "sonnen": {
            "host": config.sonnen.host,
            "port": config.sonnen.port,
            "api_version": config.sonnen.api_version.value,
            "auth_token": _secret_or_none(config.sonnen.auth_token),
            "capacity_kwh": config.sonnen.capacity_kwh,
        },
        "homewizard": {
            "car_charger": {
                "host": config.homewizard.car_charger.host,
                "port": config.homewizard.car_charger.port,
                "timeout_s": config.homewizard.car_charger.timeout_s,
                "retry_count": config.homewizard.car_charger.retry_count,
                "charging_threshold_w": config.homewizard.car_charger.charging_threshold_w,
            },
            "p1_meter": {
                "host": config.homewizard.p1_meter.host,
                "port": config.homewizard.p1_meter.port,
                "timeout_s": config.homewizard.p1_meter.timeout_s,
                "retry_count": config.homewizard.p1_meter.retry_count,
            },
            "small_solar": {
                "host": config.homewizard.small_solar.host,
                "port": config.homewizard.small_solar.port,
                "timeout_s": config.homewizard.small_solar.timeout_s,
                "retry_count": config.homewizard.small_solar.retry_count,
                "peak_w": config.homewizard.small_solar.peak_w,
            },
            "large_solar": (
                None
                if config.homewizard.large_solar is None
                else {
                    "host": config.homewizard.large_solar.host,
                    "port": config.homewizard.large_solar.port,
                    "timeout_s": config.homewizard.large_solar.timeout_s,
                    "retry_count": config.homewizard.large_solar.retry_count,
                    "peak_w": config.homewizard.large_solar.peak_w,
                }
            ),
        },
        "solaredge": {
            "host": config.solaredge.host,
            "port": config.solaredge.port,
            "timeout_s": config.solaredge.timeout_s,
            "retry_count": config.solaredge.retry_count,
            "modbus_port": config.solaredge.modbus_port,
            "unit_id": config.solaredge.unit_id,
        },
        "etrel": (
            None
            if config.etrel is None
            else {
                "host": config.etrel.host,
                "port": config.etrel.port,
                "timeout_s": config.etrel.timeout_s,
                "retry_count": config.etrel.retry_count,
                "modbus_port": config.etrel.modbus_port,
                "unit_id": config.etrel.unit_id,
            }
        ),
        "prices": {
            "provider": config.prices.provider.value,
            "api_key": _secret_or_none(config.prices.api_key),
            "area": config.prices.area,
            "csv_path": config.prices.csv_path.as_posix() if config.prices.csv_path else None,
            "base_url": config.prices.base_url,
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
            "sqlite_path": config.storage.sqlite_path.as_posix(),
            "history_retention_days": config.storage.history_retention_days,
        },
        "logging": {
            "log_dir": config.logging.log_dir.as_posix(),
            "level": config.logging.level,
            "retention_days": config.logging.retention_days,
        },
        "web": {
            "host": config.web.host,
            "port": config.web.port,
        },
    }
    if config.solar is not None:
        out["solar"] = {
            "latitude": config.solar.latitude,
            "longitude": config.solar.longitude,
            "api_key": _secret_or_none(config.solar.api_key),
            "damping_morning": config.solar.damping_morning,
            "damping_evening": config.solar.damping_evening,
            "calibration_factor": config.solar.calibration_factor,
            "planes": [
                {
                    "name": plane.name,
                    "declination": plane.declination,
                    "azimuth": plane.azimuth,
                    "kwp": plane.kwp,
                }
                for plane in config.solar.planes
            ],
        }
    return out


def _secret_or_none(value: SecretStr | None) -> str | None:
    if value is None:
        return None
    return value.get_secret_value()
