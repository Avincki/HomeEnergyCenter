"""Energy Orchestrator config editor — entry point.

Launches the tkinter GUI for editing ``config.yaml``.

    python gui.py            # edits ./config.yaml
    EO_CONFIG=foo.yaml python gui.py

The window opens with the current file's values populated. Save writes
the file atomically and keeps the previous version as ``<file>.bak``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from energy_orchestrator.config import ConfigError, load_config
from energy_orchestrator.gui.app import ConfigEditorApp


def main() -> None:
    config_path = Path(os.environ.get("EO_CONFIG", "config.yaml"))

    initial = None
    if config_path.exists():
        try:
            initial = load_config(config_path)
        except ConfigError as e:
            print(
                f"warning: existing config failed validation — opening editor anyway:\n{e}",
                file=sys.stderr,
            )

    app = ConfigEditorApp(config_path=config_path.resolve(), initial=initial)
    app.run()


if __name__ == "__main__":
    main()
