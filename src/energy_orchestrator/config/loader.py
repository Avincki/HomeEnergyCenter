from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from energy_orchestrator.config.models import AppConfig


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or is invalid."""


def load_config(path: str | Path) -> AppConfig:
    """Load and validate a YAML config file.

    Raises ConfigError with a descriptive message for missing files,
    YAML parse failures, and Pydantic validation failures.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {p}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read config file {p}: {exc}") from exc

    try:
        data: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error in {p}: {exc}") from exc

    if data is None:
        raise ConfigError(f"config file is empty: {p}")
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping, got {type(data).__name__}: {p}")

    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {p}:\n{_format_validation_error(exc)}") from exc


def _format_validation_error(exc: ValidationError) -> str:
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)
