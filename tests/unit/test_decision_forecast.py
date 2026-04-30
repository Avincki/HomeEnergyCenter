from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from energy_orchestrator.decision import (
    find_negative_injection_window_hours,
    forecast_end_soc,
    get_current_hour_price,
)
from energy_orchestrator.prices import PricePoint

NOW = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)


def _pp(hour: int, injection: float, consumption: float = 0.10) -> PricePoint:
    return PricePoint(
        timestamp=NOW.replace(hour=hour, minute=0, second=0, microsecond=0),
        consumption_eur_per_kwh=consumption,
        injection_eur_per_kwh=injection,
    )


# ----- get_current_hour_price --------------------------------------------------


def test_current_hour_price_finds_containing_window() -> None:
    prices = [_pp(11, 0.05), _pp(12, -0.02), _pp(13, 0.08)]
    p = get_current_hour_price(prices, NOW)
    assert p is not None
    assert p.timestamp.hour == 12


def test_current_hour_price_returns_none_when_now_outside_range() -> None:
    prices = [_pp(13, 0.05), _pp(14, 0.02)]  # NOW=12:00 is before
    assert get_current_hour_price(prices, NOW) is None


def test_current_hour_price_handles_unsorted_input() -> None:
    prices = [_pp(14, 0.05), _pp(11, 0.02), _pp(12, -0.01)]
    p = get_current_hour_price(prices, NOW)
    assert p is not None
    assert p.injection_eur_per_kwh == -0.01


# ----- find_negative_injection_window_hours -----------------------------------


def test_negative_window_zero_when_current_is_positive() -> None:
    prices = [_pp(12, 0.05), _pp(13, -0.02)]
    assert find_negative_injection_window_hours(prices, NOW) == 0


def test_negative_window_zero_when_no_current_price() -> None:
    prices = [_pp(13, -0.05), _pp(14, -0.02)]  # NOW=12:00, no covering point
    assert find_negative_injection_window_hours(prices, NOW) == 0


def test_negative_window_single_hour() -> None:
    prices = [_pp(12, -0.02), _pp(13, 0.04)]
    assert find_negative_injection_window_hours(prices, NOW) == 1


def test_negative_window_three_contiguous_hours() -> None:
    prices = [_pp(12, -0.02), _pp(13, -0.05), _pp(14, -0.01), _pp(15, 0.03)]
    assert find_negative_injection_window_hours(prices, NOW) == 3


def test_negative_window_stops_at_zero_price() -> None:
    """A price of exactly 0 is non-negative — window terminates."""
    prices = [_pp(12, -0.02), _pp(13, 0.0), _pp(14, -0.05)]
    assert find_negative_injection_window_hours(prices, NOW) == 1


# ----- forecast_end_soc --------------------------------------------------------


def test_forecast_zero_solar_keeps_soc_flat() -> None:
    end = forecast_end_soc(
        current_soc_pct=50.0,
        capacity_kwh=10.0,
        small_solar_w=0.0,
        window_hours=4,
    )
    assert end == pytest.approx(50.0)


def test_forecast_full_irradiance_charges_battery() -> None:
    # 1kW for 4h = 4 kWh; on a 10kWh battery that's +40% SoC
    end = forecast_end_soc(
        current_soc_pct=50.0,
        capacity_kwh=10.0,
        small_solar_w=1000.0,
        window_hours=4,
    )
    assert end == pytest.approx(90.0)


def test_forecast_can_overshoot_100_percent() -> None:
    """The function is a raw projection; clamping is the caller's concern."""
    end = forecast_end_soc(
        current_soc_pct=80.0,
        capacity_kwh=10.0,
        small_solar_w=2000.0,
        window_hours=3,
    )
    assert end == pytest.approx(80.0 + 60.0)


def test_forecast_zero_window_is_identity() -> None:
    end = forecast_end_soc(
        current_soc_pct=72.0,
        capacity_kwh=10.0,
        small_solar_w=2500.0,
        window_hours=0,
    )
    assert end == pytest.approx(72.0)


def test_negative_window_stops_at_gap_in_prices() -> None:
    """If the next sequential timestamp isn't strictly the next hour, it still
    walks until a non-negative price — the helper trusts the input ordering."""
    # h=12 (-0.02), h=13 (-0.01), then a 1-hour gap, h=15 (-0.04). The walk
    # should still count h=12, h=13, h=15 because they're all negative when
    # taken in time order.
    prices = [_pp(12, -0.02), _pp(13, -0.01), _pp(15, -0.04)]
    # Window is 3 because there's no positive price interrupting.
    assert find_negative_injection_window_hours(prices, NOW) == 3


def test_now_inside_an_hour_finds_that_hour() -> None:
    """now=12:30 should find the 12:00-13:00 hour."""
    prices = [_pp(12, -0.05), _pp(13, 0.02)]
    mid_hour = NOW + timedelta(minutes=30)
    p = get_current_hour_price(prices, mid_hour)
    assert p is not None
    assert p.timestamp.hour == 12
