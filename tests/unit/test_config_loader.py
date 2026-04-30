from __future__ import annotations

from pathlib import Path

import pytest

from energy_orchestrator.config import AppConfig, ConfigError, load_config

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_load_minimal_fixture() -> None:
    cfg = load_config(FIXTURES / "config_minimal.yaml")
    assert isinstance(cfg, AppConfig)
    assert cfg.poll_interval_s == 30
    assert cfg.sonnen.capacity_kwh == 10.0
    assert cfg.solaredge.modbus_port == 1502
    assert cfg.decision.dry_run is True  # default


def test_load_example_yaml(tmp_path: Path) -> None:
    # The shipped example must round-trip through validation.
    example = Path(__file__).resolve().parents[2] / "config.example.yaml"
    cfg = load_config(example)
    assert cfg.prices.area == "BE"


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("foo: : :\n  - bar\n")
    with pytest.raises(ConfigError, match="YAML parse error"):
        load_config(bad)


def test_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    with pytest.raises(ConfigError, match="empty"):
        load_config(empty)


def test_non_mapping_root(tmp_path: Path) -> None:
    f = tmp_path / "list.yaml"
    f.write_text("- one\n- two\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(f)


def test_validation_error_message_includes_field_path(tmp_path: Path) -> None:
    f = tmp_path / "broken.yaml"
    f.write_text(
        "poll_interval_s: 30\n"
        "sonnen:\n"
        "  host: 1.1.1.1\n"
        "  api_version: v2\n"
        "  auth_token: t\n"
        "  capacity_kwh: -5\n"  # invalid: must be > 0
        "homewizard:\n"
        "  car_charger: { host: 1.1.1.2 }\n"
        "  p1_meter: { host: 1.1.1.3 }\n"
        "  small_solar: { host: 1.1.1.4, peak_w: 2000 }\n"
        "solaredge: { host: 1.1.1.5 }\n"
        "prices: { provider: entsoe, api_key: k }\n"
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(f)
    msg = str(exc_info.value)
    assert "sonnen.capacity_kwh" in msg
