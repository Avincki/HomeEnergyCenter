from __future__ import annotations

from pathlib import Path

import pytest

from energy_orchestrator.config import load_config
from energy_orchestrator.config.models import (
    AppConfig,
    CarChargerConfig,
    DecisionConfig,
    HomeWizardConfig,
    LoggingConfig,
    P1MeterConfig,
    PricesConfig,
    PricesProvider,
    SmallSolarConfig,
    SolarEdgeConfig,
    SonnenApiVersion,
    SonnenBatterieConfig,
    StorageConfig,
    WebConfig,
)
from energy_orchestrator.gui.binding import (
    config_to_form,
    dump_yaml,
    form_to_config,
    save_with_backup,
)


def _baseline() -> AppConfig:
    return AppConfig(
        poll_interval_s=30.0,
        sonnen=SonnenBatterieConfig(
            host="192.168.1.50",
            api_version=SonnenApiVersion.V2,
            auth_token="super-secret",
            capacity_kwh=10.0,
        ),
        homewizard=HomeWizardConfig(
            car_charger=CarChargerConfig(host="192.168.1.51"),
            p1_meter=P1MeterConfig(host="192.168.1.52"),
            small_solar=SmallSolarConfig(host="192.168.1.53", peak_w=2000.0),
        ),
        solaredge=SolarEdgeConfig(host="192.168.1.60"),
        prices=PricesConfig(provider=PricesProvider.ENTSOE, api_key="entsoe-token"),
        decision=DecisionConfig(),
        storage=StorageConfig(),
        logging=LoggingConfig(),
        web=WebConfig(),
    )


# ----- config_to_form ---------------------------------------------------------


def test_config_to_form_unwraps_secrets() -> None:
    form = config_to_form(_baseline())
    assert form["sonnen.auth_token"] == "super-secret"
    assert form["prices.api_key"] == "entsoe-token"


def test_config_to_form_renders_paths_as_posix() -> None:
    form = config_to_form(_baseline())
    assert form["storage.sqlite_path"].endswith("orchestrator.db")
    assert "/" in form["storage.sqlite_path"] or "\\" not in form["storage.sqlite_path"]


def test_config_to_form_renders_enums_as_values() -> None:
    form = config_to_form(_baseline())
    assert form["sonnen.api_version"] == "v2"
    assert form["prices.provider"] == "entsoe"


def test_config_to_form_emits_empty_string_for_none() -> None:
    config = _baseline().model_copy(
        update={
            "prices": _baseline().prices.model_copy(update={"csv_path": None}),
        }
    )
    form = config_to_form(config)
    assert form["prices.csv_path"] == ""


# ----- form_to_config ---------------------------------------------------------


def test_form_to_config_round_trip() -> None:
    original = _baseline()
    form = config_to_form(original)
    rebuilt, errors = form_to_config(form)
    assert errors == {}
    assert rebuilt is not None
    assert rebuilt == original


def test_form_to_config_returns_field_keyed_errors_on_invalid_input() -> None:
    form = config_to_form(_baseline())
    form["sonnen.host"] = ""  # forbidden by _validate_host
    form["solaredge.unit_id"] = "999"  # > 247 max
    rebuilt, errors = form_to_config(form)
    assert rebuilt is None
    assert "sonnen.host" in errors
    assert "solaredge.unit_id" in errors


def test_form_to_config_allows_blank_optional_secrets_for_csv_provider() -> None:
    form = config_to_form(_baseline())
    form["prices.provider"] = "csv"
    form["prices.api_key"] = ""
    form["prices.csv_path"] = "data/prices.csv"
    rebuilt, errors = form_to_config(form)
    assert errors == {}
    assert rebuilt is not None
    assert rebuilt.prices.api_key is None
    assert rebuilt.prices.csv_path is not None


def test_form_to_config_rejects_csv_provider_without_csv_path() -> None:
    form = config_to_form(_baseline())
    form["prices.provider"] = "csv"
    form["prices.csv_path"] = ""
    rebuilt, errors = form_to_config(form)
    assert rebuilt is None
    # The cross-field check fires on the prices model itself (root-level error).
    assert any("csv_path" in v for v in errors.values())


# ----- dump_yaml --------------------------------------------------------------


def test_dump_yaml_includes_secret_plaintext_for_round_trip() -> None:
    rendered = dump_yaml(_baseline())
    assert "super-secret" in rendered
    assert "entsoe-token" in rendered
    # Enums rendered as values, not python repr.
    assert "api_version: v2" in rendered
    assert "provider: entsoe" in rendered


def test_dump_yaml_round_trips_through_load_config(tmp_path: Path) -> None:
    target = tmp_path / "round_trip.yaml"
    target.write_text(dump_yaml(_baseline()), encoding="utf-8")
    reloaded = load_config(target)
    assert reloaded == _baseline()


# ----- save_with_backup -------------------------------------------------------


def test_save_with_backup_creates_bak_for_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("old: contents\n", encoding="utf-8")
    save_with_backup(_baseline(), target)
    assert target.exists()
    bak = target.with_suffix(".yaml.bak")
    assert bak.exists()
    assert bak.read_text(encoding="utf-8") == "old: contents\n"
    # New file is the YAML render of the config.
    assert "super-secret" in target.read_text(encoding="utf-8")


def test_save_with_backup_no_bak_when_target_does_not_exist(tmp_path: Path) -> None:
    target = tmp_path / "fresh.yaml"
    save_with_backup(_baseline(), target)
    assert target.exists()
    assert not target.with_suffix(".yaml.bak").exists()


def test_save_with_backup_creates_parent_dir(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "config.yaml"
    save_with_backup(_baseline(), target)
    assert target.exists()


def test_save_with_backup_overwrites_old_bak(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    bak = target.with_suffix(".yaml.bak")
    bak.write_text("really old\n", encoding="utf-8")
    target.write_text("just old\n", encoding="utf-8")
    save_with_backup(_baseline(), target)
    assert bak.read_text(encoding="utf-8") == "just old\n"


def test_atomic_save_does_not_leave_tmp_artifact(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    save_with_backup(_baseline(), target)
    assert not (tmp_path / "config.yaml.tmp").exists()
    # Sanity: target round-trips back.
    reloaded = load_config(target)
    assert reloaded == _baseline()


# ----- regression: unknown keys --------------------------------------------


def test_form_to_config_rejects_unknown_dotted_key() -> None:
    form = config_to_form(_baseline())
    form["sonnen.gibberish_field"] = "x"
    rebuilt, errors = form_to_config(form)
    # extra=forbid on _StrictModel surfaces this as a Pydantic error.
    assert rebuilt is None
    # The error path is "sonnen.gibberish_field" or "sonnen" depending on
    # Pydantic version. Either way it should be in errors.
    assert any("gibberish" in k or "extra" in v.lower() for k, v in errors.items())


# ----- pytest config ----------------------------------------------------------


pytestmark = pytest.mark.filterwarnings(
    "ignore::DeprecationWarning"
)  # SQLAlchemy/Pydantic chatter from imports
