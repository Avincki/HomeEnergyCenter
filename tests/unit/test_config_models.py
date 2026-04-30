from __future__ import annotations

import pytest
from pydantic import ValidationError

from energy_orchestrator.config import (
    DecisionConfig,
    HomeWizardConfig,
    PricesConfig,
    PricesProvider,
    SmallSolarConfig,
    SolarEdgeConfig,
    SonnenApiVersion,
    SonnenBatterieConfig,
)

# ----- host validation ---------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["192.168.1.50", "10.0.0.1", "homewizard.local", "fe80::1", "host-1.example.com"],
)
def test_device_host_accepts_ipv4_ipv6_and_hostnames(host: str) -> None:
    cfg = SonnenBatterieConfig(
        host=host, api_version=SonnenApiVersion.V2, auth_token="t", capacity_kwh=10.0
    )
    assert cfg.host == host


@pytest.mark.parametrize("host", ["", "   ", "bad host", "-leading.dash", "trailing-.dash"])
def test_device_host_rejects_garbage(host: str) -> None:
    with pytest.raises(ValidationError):
        SonnenBatterieConfig(host=host, auth_token="t", capacity_kwh=10.0)


# ----- port validation ---------------------------------------------------------


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_port_out_of_range(port: int) -> None:
    with pytest.raises(ValidationError):
        SonnenBatterieConfig(host="1.1.1.1", port=port, auth_token="t", capacity_kwh=10.0)


# ----- sonnen ------------------------------------------------------------------


def test_sonnen_v2_requires_token() -> None:
    with pytest.raises(ValidationError, match="auth_token"):
        SonnenBatterieConfig(host="1.1.1.1", api_version=SonnenApiVersion.V2, capacity_kwh=10.0)


def test_sonnen_v1_token_optional() -> None:
    cfg = SonnenBatterieConfig(
        host="1.1.1.1", api_version=SonnenApiVersion.V1, capacity_kwh=10.0, port=8080
    )
    assert cfg.auth_token is None


def test_sonnen_capacity_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        SonnenBatterieConfig(host="1.1.1.1", auth_token="t", capacity_kwh=0)


# ----- solaredge ---------------------------------------------------------------


def test_solaredge_defaults() -> None:
    cfg = SolarEdgeConfig(host="1.1.1.1")
    assert cfg.modbus_port == 1502
    assert cfg.unit_id == 1


def test_solaredge_unit_id_range() -> None:
    with pytest.raises(ValidationError):
        SolarEdgeConfig(host="1.1.1.1", unit_id=0)
    with pytest.raises(ValidationError):
        SolarEdgeConfig(host="1.1.1.1", unit_id=248)


# ----- prices ------------------------------------------------------------------


@pytest.mark.parametrize("provider", [PricesProvider.ENTSOE, PricesProvider.TIBBER])
def test_prices_external_provider_requires_api_key(provider: PricesProvider) -> None:
    with pytest.raises(ValidationError, match="api_key"):
        PricesConfig(provider=provider)


def test_prices_csv_requires_path() -> None:
    with pytest.raises(ValidationError, match="csv_path"):
        PricesConfig(provider=PricesProvider.CSV)


def test_prices_csv_path_accepted() -> None:
    cfg = PricesConfig(provider=PricesProvider.CSV, csv_path="data/prices.csv")
    assert str(cfg.csv_path) == "data/prices.csv" or str(cfg.csv_path).endswith("prices.csv")


# ----- decision ----------------------------------------------------------------


def test_decision_defaults_match_spec() -> None:
    cfg = DecisionConfig()
    assert cfg.battery_low_soc_pct == 60.0
    assert cfg.battery_full_soc_pct == 80.0
    assert cfg.hysteresis_pct == 5.0
    assert cfg.dry_run is True


def test_decision_low_must_be_below_full() -> None:
    with pytest.raises(ValidationError, match="must be < battery_full_soc_pct"):
        DecisionConfig(battery_low_soc_pct=80, battery_full_soc_pct=70)


def test_decision_hysteresis_cannot_overrun_full() -> None:
    with pytest.raises(ValidationError, match="overruns the full threshold"):
        DecisionConfig(battery_low_soc_pct=70, battery_full_soc_pct=75, hysteresis_pct=10)


def test_decision_percent_bounds() -> None:
    with pytest.raises(ValidationError):
        DecisionConfig(battery_low_soc_pct=-1)
    with pytest.raises(ValidationError):
        DecisionConfig(battery_full_soc_pct=101)


# ----- homewizard --------------------------------------------------------------


def test_small_solar_peak_required() -> None:
    with pytest.raises(ValidationError):
        SmallSolarConfig(host="1.1.1.1")  # type: ignore[call-arg]


def test_homewizard_block() -> None:
    cfg = HomeWizardConfig(
        car_charger={"host": "1.1.1.1"},  # type: ignore[arg-type]
        p1_meter={"host": "1.1.1.2"},  # type: ignore[arg-type]
        small_solar={"host": "1.1.1.3", "peak_w": 2000},  # type: ignore[arg-type]
    )
    assert cfg.car_charger.charging_threshold_w == 500.0  # default
    assert cfg.small_solar.peak_w == 2000


# ----- immutability + extra-forbid --------------------------------------------


def test_models_are_frozen() -> None:
    cfg = DecisionConfig()
    with pytest.raises(ValidationError):
        cfg.dry_run = False  # type: ignore[misc]


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        DecisionConfig(unknown_field=1)  # type: ignore[call-arg]
