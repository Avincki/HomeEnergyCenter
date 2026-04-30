from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from energy_orchestrator.config.models import DecisionConfig
from energy_orchestrator.data.models import DecisionState, OverrideMode
from energy_orchestrator.decision import (
    DecisionEngine,
    OverrideState,
    Rule,
    RuleOutcome,
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


def _config() -> DecisionConfig:
    return DecisionConfig(
        battery_low_soc_pct=60.0,
        battery_full_soc_pct=80.0,
        hysteresis_pct=5.0,
    )


def _pp(hour: int, injection: float) -> PricePoint:
    return PricePoint(
        timestamp=NOW.replace(hour=hour, minute=0, second=0, microsecond=0),
        consumption_eur_per_kwh=0.10,
        injection_eur_per_kwh=injection,
    )


# ----- override AUTO disallowed in OverrideState ------------------------------


def test_override_state_rejects_auto_mode() -> None:
    with pytest.raises(ValueError, match="AUTO"):
        OverrideState(mode=OverrideMode.AUTO)


def test_override_expiry_marks_inactive() -> None:
    expired = OverrideState(mode=OverrideMode.FORCE_ON, expires_at=NOW - timedelta(seconds=1))
    assert expired.is_active(NOW) is False


def test_override_indefinite_always_active() -> None:
    forever = OverrideState(mode=OverrideMode.FORCE_OFF, expires_at=None)
    assert forever.is_active(NOW) is True


# ----- engine integration -----------------------------------------------------


def test_engine_runs_rules_in_priority_order_battery_wins() -> None:
    engine = DecisionEngine(_config())
    # SoC 50, was ON, car also charging. Rule 1 should win.
    record = engine.decide(
        _ctx(battery_soc_pct=50.0, previous_state=DecisionState.ON, car_is_charging=True)
    )
    assert record.rule_fired == "battery_low_soc"
    assert record.state is DecisionState.ON


def test_engine_falls_through_to_rule_2() -> None:
    engine = DecisionEngine(_config())
    record = engine.decide(_ctx(battery_soc_pct=70.0, car_is_charging=True))
    assert record.rule_fired == "car_charging"
    assert record.state is DecisionState.ON


def test_engine_falls_through_to_rule_3() -> None:
    engine = DecisionEngine(_config())
    record = engine.decide(_ctx(battery_soc_pct=70.0, prices=[_pp(12, 0.05)]))
    assert record.rule_fired == "positive_injection_price"
    assert record.state is DecisionState.ON


def test_engine_falls_through_to_rule_4_off_when_no_negative_window() -> None:
    engine = DecisionEngine(_config())
    record = engine.decide(_ctx(battery_soc_pct=70.0, prices=[_pp(12, 0.0)]))
    # Rule 3 doesn't fire (price not strictly > 0); rule 4 fallback OFF
    assert record.rule_fired == "negative_window_forecast"
    assert record.state is DecisionState.OFF


def test_engine_rule_4_on_with_headroom() -> None:
    engine = DecisionEngine(_config())
    # SoC=60 (above the 55 hysteresis-shifted low threshold), 2-hour negative
    # window, 500W solar, 10kWh capacity -> end SoC = 60 + 10 = 70 < 80 (full).
    prices = [_pp(12, -0.02), _pp(13, -0.03), _pp(14, 0.01)]
    record = engine.decide(_ctx(battery_soc_pct=60.0, small_solar_w=500.0, prices=prices))
    assert record.rule_fired == "negative_window_forecast"
    assert record.state is DecisionState.ON
    assert record.forecast_end_soc_pct == 70.0


def test_engine_rule_4_off_when_battery_would_saturate() -> None:
    engine = DecisionEngine(_config())
    # SoC=70 (above hysteresis threshold), 5-hour negative window, 1kW solar
    # -> end SoC = 70 + 50 = 120%, way above full 80% -> OFF.
    prices = [_pp(h, -0.02) for h in range(12, 17)] + [_pp(17, 0.01)]
    record = engine.decide(_ctx(battery_soc_pct=70.0, small_solar_w=1000.0, prices=prices))
    assert record.rule_fired == "negative_window_forecast"
    assert record.state is DecisionState.OFF
    assert record.forecast_end_soc_pct == 120.0


# ----- override semantics -----------------------------------------------------


def test_override_force_off_overrides_auto_on() -> None:
    engine = DecisionEngine(_config())
    record = engine.decide(
        _ctx(
            battery_soc_pct=50.0,  # rule 1 says ON
            previous_state=DecisionState.OFF,
            override=OverrideState(mode=OverrideMode.FORCE_OFF),
        )
    )
    assert record.state is DecisionState.OFF
    assert record.manual_override is True
    assert record.override_mode is OverrideMode.FORCE_OFF
    # Audit trail still describes auto's decision
    assert record.rule_fired == "battery_low_soc"


def test_override_force_on_overrides_auto_off() -> None:
    engine = DecisionEngine(_config())
    record = engine.decide(
        _ctx(
            battery_soc_pct=70.0,  # rule 4 fallback OFF
            prices=[_pp(12, 0.0)],
            override=OverrideState(mode=OverrideMode.FORCE_ON),
        )
    )
    assert record.state is DecisionState.ON
    assert record.manual_override is True
    assert record.override_mode is OverrideMode.FORCE_ON
    assert record.rule_fired == "negative_window_forecast"


def test_expired_override_does_not_apply() -> None:
    engine = DecisionEngine(_config())
    record = engine.decide(
        _ctx(
            battery_soc_pct=50.0,  # rule 1 -> ON
            previous_state=DecisionState.OFF,
            override=OverrideState(
                mode=OverrideMode.FORCE_OFF,
                expires_at=NOW - timedelta(seconds=1),
            ),
        )
    )
    assert record.state is DecisionState.ON
    assert record.manual_override is False
    assert record.override_mode is None


# ----- state_changed flag -----------------------------------------------------


def test_state_changed_true_on_transition() -> None:
    engine = DecisionEngine(_config())
    record = engine.decide(_ctx(battery_soc_pct=50.0, previous_state=DecisionState.OFF))
    assert record.state is DecisionState.ON
    assert record.state_changed is True


def test_state_changed_false_when_holding() -> None:
    engine = DecisionEngine(_config())
    record = engine.decide(_ctx(battery_soc_pct=50.0, previous_state=DecisionState.ON))
    assert record.state is DecisionState.ON
    assert record.state_changed is False


def test_state_changed_compares_against_applied_state_not_auto() -> None:
    """When override flips us from auto's ON to OFF, state_changed should
    reflect the *applied* OFF transition vs previous_state."""
    engine = DecisionEngine(_config())
    record = engine.decide(
        _ctx(
            battery_soc_pct=50.0,  # auto would be ON
            previous_state=DecisionState.ON,
            override=OverrideState(mode=OverrideMode.FORCE_OFF),
        )
    )
    assert record.state is DecisionState.OFF
    assert record.state_changed is True


# ----- custom rule ordering ---------------------------------------------------


class _AlwaysOnRule(Rule):
    name = "always_on"

    def evaluate(self, ctx: TickContext, config: DecisionConfig) -> RuleOutcome | None:
        return RuleOutcome(state=DecisionState.ON, reason="test stub")


def test_engine_with_custom_rule_chain() -> None:
    engine = DecisionEngine(_config(), rules=[_AlwaysOnRule()])
    record = engine.decide(_ctx(battery_soc_pct=99.0, prices=[_pp(12, 0.0)]))
    assert record.rule_fired == "always_on"
    assert record.state is DecisionState.ON


def test_engine_raises_when_no_rule_claims() -> None:
    class _SilentRule(Rule):
        name = "silent"

        def evaluate(self, ctx: TickContext, config: DecisionConfig) -> RuleOutcome | None:
            return None

    engine = DecisionEngine(_config(), rules=[_SilentRule()])
    with pytest.raises(RuntimeError, match="no rule"):
        engine.decide(_ctx())
