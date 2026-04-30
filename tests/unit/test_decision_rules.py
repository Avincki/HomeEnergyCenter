from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from energy_orchestrator.config.models import DecisionConfig
from energy_orchestrator.data.models import DecisionState
from energy_orchestrator.decision import (
    BatteryLowRule,
    CarChargingRule,
    NegativeWindowForecastRule,
    PositiveInjectionRule,
    TickContext,
)
from energy_orchestrator.prices import PricePoint

NOW = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)


def _ctx(**overrides: Any) -> TickContext:
    defaults: dict[str, Any] = {
        "timestamp": NOW,
        "battery_soc_pct": 70.0,
        "car_is_charging": False,
        "small_solar_w": 0.0,
        "prices": [],
        "previous_state": DecisionState.OFF,
        "battery_capacity_kwh": 10.0,
        "override": None,
    }
    defaults.update(overrides)
    return TickContext(**defaults)


def _config(
    *,
    low: float = 60.0,
    full: float = 80.0,
    hysteresis: float = 5.0,
) -> DecisionConfig:
    return DecisionConfig(
        battery_low_soc_pct=low,
        battery_full_soc_pct=full,
        hysteresis_pct=hysteresis,
    )


def _pp(hour: int, injection: float, consumption: float = 0.10) -> PricePoint:
    return PricePoint(
        timestamp=NOW.replace(hour=hour, minute=0, second=0, microsecond=0),
        consumption_eur_per_kwh=consumption,
        injection_eur_per_kwh=injection,
    )


# ----- BatteryLowRule ----------------------------------------------------------


def test_battery_low_fires_below_lower_band_when_previous_off() -> None:
    rule = BatteryLowRule()
    # Previously OFF -> threshold = 60 - 5 = 55
    out = rule.evaluate(_ctx(battery_soc_pct=54.9, previous_state=DecisionState.OFF), _config())
    assert out is not None
    assert out.state is DecisionState.ON


def test_battery_low_does_not_fire_just_above_lower_band_when_previous_off() -> None:
    rule = BatteryLowRule()
    out = rule.evaluate(_ctx(battery_soc_pct=55.0, previous_state=DecisionState.OFF), _config())
    assert out is None


def test_battery_low_fires_below_upper_band_when_previous_on() -> None:
    rule = BatteryLowRule()
    # Previously ON -> threshold = 60 + 5 = 65; sticky.
    out = rule.evaluate(_ctx(battery_soc_pct=64.9, previous_state=DecisionState.ON), _config())
    assert out is not None
    assert out.state is DecisionState.ON


def test_battery_low_releases_at_upper_band_when_previous_on() -> None:
    rule = BatteryLowRule()
    out = rule.evaluate(_ctx(battery_soc_pct=65.0, previous_state=DecisionState.ON), _config())
    assert out is None


def test_battery_low_does_not_fire_well_above() -> None:
    rule = BatteryLowRule()
    out = rule.evaluate(_ctx(battery_soc_pct=80.0, previous_state=DecisionState.ON), _config())
    assert out is None


def test_battery_low_no_previous_state_uses_off_threshold() -> None:
    rule = BatteryLowRule()
    out = rule.evaluate(_ctx(battery_soc_pct=58.0, previous_state=None), _config())
    assert out is None  # 58 > 55, doesn't fire


# ----- CarChargingRule ---------------------------------------------------------


def test_car_charging_fires_when_charging() -> None:
    rule = CarChargingRule()
    out = rule.evaluate(_ctx(car_is_charging=True), _config())
    assert out is not None
    assert out.state is DecisionState.ON


def test_car_charging_silent_when_idle() -> None:
    rule = CarChargingRule()
    out = rule.evaluate(_ctx(car_is_charging=False), _config())
    assert out is None


# ----- PositiveInjectionRule ---------------------------------------------------


def test_positive_injection_fires_on_positive_price() -> None:
    rule = PositiveInjectionRule()
    out = rule.evaluate(_ctx(prices=[_pp(12, 0.05)]), _config())
    assert out is not None
    assert out.state is DecisionState.ON


def test_positive_injection_silent_on_zero_price() -> None:
    rule = PositiveInjectionRule()
    out = rule.evaluate(_ctx(prices=[_pp(12, 0.0)]), _config())
    assert out is None


def test_positive_injection_silent_on_negative_price() -> None:
    rule = PositiveInjectionRule()
    out = rule.evaluate(_ctx(prices=[_pp(12, -0.02)]), _config())
    assert out is None


def test_positive_injection_silent_when_no_current_price() -> None:
    rule = PositiveInjectionRule()
    # NOW=12:00, only a 13:00 price -> no current
    out = rule.evaluate(_ctx(prices=[_pp(13, 0.05)]), _config())
    assert out is None


# ----- NegativeWindowForecastRule ---------------------------------------------


def test_forecast_no_negative_window_returns_off() -> None:
    rule = NegativeWindowForecastRule()
    out = rule.evaluate(_ctx(prices=[_pp(12, 0.05)]), _config())
    assert out is not None
    assert out.state is DecisionState.OFF
    assert out.forecast_end_soc_pct is None


def test_forecast_headroom_returns_on() -> None:
    rule = NegativeWindowForecastRule()
    # 4-hour negative window, 1kW small solar, 10kWh battery, current 50%
    # End SoC = 50 + 40 = 90 — wait, that exceeds 80 (full). So this should be OFF.
    # Use a smaller window or solar to keep below full.
    # 2-hour window, 1kW, 10kWh, current 50% => +20% = 70, < 80 (full) -> ON
    prices = [_pp(12, -0.02), _pp(13, -0.05), _pp(14, 0.01)]
    out = rule.evaluate(_ctx(battery_soc_pct=50.0, small_solar_w=1000.0, prices=prices), _config())
    assert out is not None
    assert out.state is DecisionState.ON
    assert out.forecast_end_soc_pct == 70.0


def test_forecast_saturation_returns_off() -> None:
    rule = NegativeWindowForecastRule()
    # 5-hour window, 1kW, 10kWh, current 60% => +50% = 110%, well above full -> OFF
    prices = [_pp(h, -0.02) for h in range(12, 17)] + [_pp(17, 0.01)]
    out = rule.evaluate(_ctx(battery_soc_pct=60.0, small_solar_w=1000.0, prices=prices), _config())
    assert out is not None
    assert out.state is DecisionState.OFF
    assert out.forecast_end_soc_pct == 110.0


def test_forecast_zero_solar_means_battery_static_so_on() -> None:
    rule = NegativeWindowForecastRule()
    # No small-solar production -> SoC won't budge; still below full -> ON
    prices = [_pp(12, -0.02), _pp(13, 0.05)]
    out = rule.evaluate(_ctx(battery_soc_pct=70.0, small_solar_w=0.0, prices=prices), _config())
    assert out is not None
    assert out.state is DecisionState.ON
    assert out.forecast_end_soc_pct == 70.0
