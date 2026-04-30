"""The four rules that drive SolarEdge ON/OFF, evaluated in priority order
by the engine. Each rule returns a ``RuleOutcome`` to claim the decision or
``None`` to defer to the next rule. Rule 4 always claims (it is the fallback).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from energy_orchestrator.config.models import DecisionConfig
from energy_orchestrator.data.models import DecisionState
from energy_orchestrator.decision.context import TickContext
from energy_orchestrator.decision.forecast import (
    find_negative_injection_window_hours,
    forecast_end_soc,
    get_current_hour_price,
)


@dataclass(frozen=True)
class RuleOutcome:
    state: DecisionState
    reason: str
    forecast_end_soc_pct: float | None = None


class Rule(ABC):
    name: ClassVar[str]

    @abstractmethod
    def evaluate(self, ctx: TickContext, config: DecisionConfig) -> RuleOutcome | None:
        """Return an outcome to claim the decision, or None to defer."""


class BatteryLowRule(Rule):
    """Rule 1: ON if SoC is below the low threshold (with hysteresis).

    Hysteresis is applied around the configured ``battery_low_soc_pct``: while
    we were OFF last tick, we re-engage only at ``low - hysteresis``; while we
    were ON, we keep engaging until ``low + hysteresis``. This prevents
    flapping when SoC oscillates around the threshold.
    """

    name: ClassVar[str] = "battery_low_soc"

    def evaluate(self, ctx: TickContext, config: DecisionConfig) -> RuleOutcome | None:
        if ctx.previous_state is DecisionState.ON:
            threshold = config.battery_low_soc_pct + config.hysteresis_pct
        else:
            threshold = config.battery_low_soc_pct - config.hysteresis_pct
        if ctx.battery_soc_pct < threshold:
            return RuleOutcome(
                state=DecisionState.ON,
                reason=(
                    f"battery SoC {ctx.battery_soc_pct:.1f}% < {threshold:.1f}% "
                    f"(low {config.battery_low_soc_pct:.0f}% ± "
                    f"hysteresis {config.hysteresis_pct:.0f}%)"
                ),
            )
        return None


class CarChargingRule(Rule):
    """Rule 2: ON if the EV is currently drawing power above the configured threshold."""

    name: ClassVar[str] = "car_charging"

    def evaluate(self, ctx: TickContext, config: DecisionConfig) -> RuleOutcome | None:
        if ctx.car_is_charging:
            return RuleOutcome(
                state=DecisionState.ON,
                reason="car charger drawing power above threshold",
            )
        return None


class PositiveInjectionRule(Rule):
    """Rule 3: ON if the current hour's injection price is strictly > 0."""

    name: ClassVar[str] = "positive_injection_price"

    def evaluate(self, ctx: TickContext, config: DecisionConfig) -> RuleOutcome | None:
        current = get_current_hour_price(ctx.prices, ctx.timestamp)
        if current is None:
            return None
        if current.injection_eur_per_kwh > 0:
            return RuleOutcome(
                state=DecisionState.ON,
                reason=(f"injection price {current.injection_eur_per_kwh:.4f} EUR/kWh > 0"),
            )
        return None


class NegativeWindowForecastRule(Rule):
    """Rule 4 (fallback): forecast SoC over the upcoming negative-injection
    window and decide ON if the battery has headroom, OFF if it would saturate.

    Always returns an outcome. If no negative window is detected, it falls back
    to OFF (we shouldn't produce when injection is non-positive and rules 1-3
    didn't fire — that means the user is exporting at a loss).
    """

    name: ClassVar[str] = "negative_window_forecast"

    def evaluate(self, ctx: TickContext, config: DecisionConfig) -> RuleOutcome | None:
        window_hours = find_negative_injection_window_hours(ctx.prices, ctx.timestamp)
        if window_hours == 0:
            return RuleOutcome(
                state=DecisionState.OFF,
                reason="no negative-injection-price window detected; default OFF",
            )

        end_soc = forecast_end_soc(
            current_soc_pct=ctx.battery_soc_pct,
            capacity_kwh=ctx.battery_capacity_kwh,
            small_solar_w=ctx.small_solar_w,
            window_hours=window_hours,
        )

        full = config.battery_full_soc_pct
        if end_soc < full:
            return RuleOutcome(
                state=DecisionState.ON,
                reason=(
                    f"forecast end-SoC {end_soc:.1f}% < full {full:.0f}% over "
                    f"{window_hours}h negative-price window — produce and store"
                ),
                forecast_end_soc_pct=end_soc,
            )
        return RuleOutcome(
            state=DecisionState.OFF,
            reason=(
                f"forecast end-SoC {end_soc:.1f}% >= full {full:.0f}% over "
                f"{window_hours}h negative-price window — battery would saturate, curtail"
            ),
            forecast_end_soc_pct=end_soc,
        )


DEFAULT_RULES: tuple[Rule, ...] = (
    BatteryLowRule(),
    CarChargingRule(),
    PositiveInjectionRule(),
    NegativeWindowForecastRule(),
)
