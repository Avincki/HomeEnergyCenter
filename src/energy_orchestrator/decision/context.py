"""Input/output shapes for the decision engine."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from energy_orchestrator.data.models import DecisionState, OverrideMode
from energy_orchestrator.prices import PricePoint


@dataclass(frozen=True)
class OverrideState:
    """Active manual override. ``None`` (not an instance with mode=AUTO)
    represents the no-override case."""

    mode: OverrideMode
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.mode is OverrideMode.AUTO:
            raise ValueError("OverrideState only represents active overrides; pass None for AUTO")

    def is_active(self, now: datetime) -> bool:
        return self.expires_at is None or now < self.expires_at

    @property
    def forced_state(self) -> DecisionState:
        if self.mode is OverrideMode.FORCE_ON:
            return DecisionState.ON
        return DecisionState.OFF


@dataclass(frozen=True)
class TickContext:
    """One tick's input data for the decision engine.

    The orchestrator gathers all readings and prices, computes derived flags
    (e.g. ``car_is_charging``), and constructs this struct. The engine assumes
    everything in here is already validated — missing-essential-data ticks
    must be skipped *before* reaching the engine, per spec.
    """

    timestamp: datetime
    battery_soc_pct: float
    car_is_charging: bool
    small_solar_w: float
    prices: Sequence[PricePoint]
    previous_state: DecisionState | None
    battery_capacity_kwh: float
    override: OverrideState | None = None


@dataclass(frozen=True)
class DecisionRecord:
    """Output of one decision-engine tick. Maps cleanly onto the ``Decision`` DB row.

    ``state`` is the *applied* state (override-aware). ``rule_fired`` /
    ``reason`` / ``forecast_end_soc_pct`` always describe the auto computation,
    even when the override wins — so the audit trail records what the engine
    *would have* decided.
    """

    timestamp: datetime
    state: DecisionState
    rule_fired: str
    reason: str
    state_changed: bool
    manual_override: bool
    override_mode: OverrideMode | None
    forecast_end_soc_pct: float | None
