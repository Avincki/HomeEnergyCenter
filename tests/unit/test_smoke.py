from __future__ import annotations

import energy_orchestrator


def test_package_importable() -> None:
    assert energy_orchestrator.__version__ == "0.1.0"
