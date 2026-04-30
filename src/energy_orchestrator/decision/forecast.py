"""Pure helpers for rule 4's forecast: how long is the upcoming
negative-injection-price window, and where will the battery end up if only
the small-solar string charges it during that window."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from energy_orchestrator.prices import PricePoint


def get_current_hour_price(prices: Sequence[PricePoint], now: datetime) -> PricePoint | None:
    """Return the price point whose hour-window contains ``now``, or None."""
    for p in sorted(prices, key=lambda pp: pp.timestamp):
        if p.timestamp <= now < p.timestamp + timedelta(hours=1):
            return p
    return None


def find_negative_injection_window_hours(prices: Sequence[PricePoint], now: datetime) -> int:
    """Count contiguous upcoming hours (including the current one) with
    negative injection price.

    Returns 0 if the current hour has non-negative injection or if no price
    point covers ``now``.
    """
    sorted_prices = sorted(prices, key=lambda pp: pp.timestamp)
    current_idx: int | None = None
    for i, p in enumerate(sorted_prices):
        if p.timestamp <= now < p.timestamp + timedelta(hours=1):
            current_idx = i
            break
    if current_idx is None:
        return 0
    if sorted_prices[current_idx].injection_eur_per_kwh >= 0:
        return 0

    hours = 0
    for p in sorted_prices[current_idx:]:
        if p.injection_eur_per_kwh < 0:
            hours += 1
        else:
            break
    return hours


def forecast_end_soc(
    *,
    current_soc_pct: float,
    capacity_kwh: float,
    small_solar_w: float,
    window_hours: int,
) -> float:
    """Project SoC at the end of ``window_hours`` charging at constant
    ``small_solar_w`` from the small (non-SolarEdge) string only.
    """
    energy_kwh = (small_solar_w / 1000.0) * window_hours
    soc_delta_pct = (energy_kwh / capacity_kwh) * 100.0
    return current_soc_pct + soc_delta_pct
