"""Energy Orchestrator entry point.

Right-click in PyCharm -> Run, or from the command line:

    python main.py

Reads ``config.yaml`` from the current directory (override with the
``EO_CONFIG`` env var). Bind host/port come from the ``web:`` block of
config.yaml.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

from energy_orchestrator.config import ConfigError, load_config
from energy_orchestrator.monitoring import configure_logging


def main() -> None:
    config_path = Path(os.environ.get("EO_CONFIG", "config.yaml"))
    if not config_path.exists():
        example = Path("config.example.yaml")
        hint = f" — copy {example} and edit it" if example.exists() else ""
        print(f"config not found: {config_path}{hint}", file=sys.stderr)
        sys.exit(1)

    try:
        config = load_config(config_path)
    except ConfigError as e:
        print(f"config error:\n{e}", file=sys.stderr)
        sys.exit(1)

    # Configure structlog + stdlib logging before uvicorn boots so its own
    # access logger flows through our handlers (with log_config=None below).
    configure_logging(config.logging)

    # The factory re-loads config from EO_CONFIG when uvicorn calls it; make
    # sure both sides see the same path even if cwd differs.
    os.environ["EO_CONFIG"] = str(config_path.resolve())

    uvicorn.run(
        "energy_orchestrator.web.app:create_app",
        factory=True,
        host=config.web.host,
        port=config.web.port,
        log_config=None,  # use the root logger we just configured
    )


if __name__ == "__main__":
    main()
