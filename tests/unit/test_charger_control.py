from __future__ import annotations

from datetime import UTC, datetime

from energy_orchestrator.config.models import ChargerControlConfig
from energy_orchestrator.decision.charger_control import (
    ChargerController,
    ChargerInputs,
    is_daytime,
)

# Brussels-ish coordinates for the daytime tests.
_LAT, _LON = 50.85, 4.35


def _config(**overrides: object) -> ChargerControlConfig:
    return ChargerControlConfig(enabled=True, dry_run=False, **overrides)  # type: ignore[arg-type]


def _inputs(**overrides: object) -> ChargerInputs:
    base: dict[str, object] = {
        "timestamp": datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
        "is_daytime": True,
        "car_attached": True,
        "actual_current_a": None,
        "battery_soc_pct": 50.0,
        "grid_power_w": 0.0,
        "battery_power_w": 0.0,
    }
    base.update(overrides)
    return ChargerInputs(**base)  # type: ignore[arg-type]


# ----- eligibility gates -------------------------------------------------------


def test_pauses_outside_daytime() -> None:
    ctrl = ChargerController(_config())
    cmd = ctrl.decide(_inputs(is_daytime=False))
    assert cmd.paused and cmd.target_a == 0.0


def test_pauses_when_no_car_attached() -> None:
    ctrl = ChargerController(_config())
    cmd = ctrl.decide(_inputs(car_attached=False))
    assert cmd.paused and cmd.target_a == 0.0


def test_pauses_below_battery_soc_floor() -> None:
    ctrl = ChargerController(_config())  # floor 30, hysteresis 3
    cmd = ctrl.decide(_inputs(battery_soc_pct=25.0))
    assert cmd.paused


def test_soc_floor_hysteresis() -> None:
    ctrl = ChargerController(_config())  # floor 30, hyst 3 -> re-enable at 33
    # Enable with a strong signal (battery idle -> reserve 9 kW).
    assert not ctrl.decide(_inputs(battery_soc_pct=50.0)).paused
    # Drop just below the floor -> disabled.
    assert ctrl.decide(_inputs(battery_soc_pct=29.0)).paused
    # Back between floor and floor+hysteresis -> still disabled (latched).
    assert ctrl.decide(_inputs(battery_soc_pct=31.0)).paused
    # Above floor + hysteresis -> re-enabled.
    assert not ctrl.decide(_inputs(battery_soc_pct=34.0)).paused


# ----- resume from pause -------------------------------------------------------


def test_resume_to_min_when_signal_covers_it() -> None:
    ctrl = ChargerController(_config())  # resume threshold 4300, min 6
    # Battery idle -> reserve 9 kW -> signal well over the resume threshold.
    cmd = ctrl.decide(_inputs(grid_power_w=0.0, battery_power_w=0.0))
    assert not cmd.paused and cmd.target_a == 6.0


def test_resume_blocked_when_signal_too_low() -> None:
    ctrl = ChargerController(_config())
    # Battery already discharging at max (9 kW) -> reserve 0; export only 4000 W
    # -> signal 4000 < 4300 resume threshold.
    cmd = ctrl.decide(_inputs(grid_power_w=-4000.0, battery_power_w=9000.0))
    assert cmd.paused and cmd.target_a == 0.0


def test_resume_blocked_while_importing() -> None:
    ctrl = ChargerController(_config())
    # Big virtual signal (reserve) but actually importing -> stay paused.
    cmd = ctrl.decide(_inputs(grid_power_w=600.0, battery_power_w=0.0))
    assert cmd.paused


# ----- tracking ----------------------------------------------------------------


def test_uptick_on_surplus_while_drawing() -> None:
    ctrl = ChargerController(_config())
    ctrl._target_a = 10.0  # white-box: pretend mid-session
    cmd = ctrl.decide(_inputs(grid_power_w=-600.0, battery_power_w=9000.0, actual_current_a=10.0))
    # export 600 + reserve 0 = 600 > 500 export threshold, car drawing -> +1 A.
    assert not cmd.paused and cmd.target_a == 11.0


def test_downtick_on_real_import_takes_priority() -> None:
    ctrl = ChargerController(_config())
    ctrl._target_a = 10.0
    # Importing 600 W AND a large virtual signal (reserve). Import wins -> -1 A.
    cmd = ctrl.decide(_inputs(grid_power_w=600.0, battery_power_w=0.0, actual_current_a=10.0))
    assert cmd.target_a == 9.0


def test_anti_windup_holds_when_not_drawing() -> None:
    ctrl = ChargerController(_config())
    ctrl._target_a = 10.0
    # Surplus says ramp up, but the car isn't drawing (full/clamped) -> hold.
    cmd = ctrl.decide(_inputs(grid_power_w=0.0, battery_power_w=0.0, actual_current_a=0.0))
    assert cmd.target_a == 10.0


def test_downtick_below_min_pauses() -> None:
    ctrl = ChargerController(_config())
    ctrl._target_a = 6.0
    cmd = ctrl.decide(_inputs(grid_power_w=600.0, battery_power_w=0.0, actual_current_a=6.0))
    assert cmd.paused and cmd.target_a == 0.0


def test_uptick_clamped_to_max() -> None:
    ctrl = ChargerController(_config())  # max 16
    ctrl._target_a = 16.0
    cmd = ctrl.decide(_inputs(grid_power_w=-5000.0, battery_power_w=0.0, actual_current_a=16.0))
    assert cmd.target_a == 16.0


def test_holds_within_deadband() -> None:
    ctrl = ChargerController(_config())
    ctrl._target_a = 10.0
    # Signal exactly the battery reserve minus near-max discharge: keep it in
    # the dead-band (>import threshold check fails, <=export threshold).
    cmd = ctrl.decide(_inputs(grid_power_w=0.0, battery_power_w=8700.0, actual_current_a=10.0))
    # export 0 + reserve (9000-8700=300) = 300, not > 500 export, no import -> hold.
    assert cmd.target_a == 10.0


# ----- available-power signal: battery reserve ---------------------------------


def test_battery_reserve_enables_charging_without_export() -> None:
    ctrl = ChargerController(_config())
    # No grid export at all, battery idle -> reserve 9 kW alone resumes charging.
    cmd = ctrl.decide(_inputs(grid_power_w=0.0, battery_power_w=0.0))
    assert not cmd.paused and cmd.target_a == 6.0


def test_no_reserve_when_battery_maxed_and_no_export() -> None:
    ctrl = ChargerController(_config())
    # Battery already at max discharge, no export -> signal 0 -> can't resume.
    cmd = ctrl.decide(_inputs(grid_power_w=0.0, battery_power_w=9000.0))
    assert cmd.paused


# ----- daytime helper ----------------------------------------------------------


def test_is_daytime_true_at_local_noon() -> None:
    assert is_daytime(datetime(2026, 5, 22, 12, 0, tzinfo=UTC), _LAT, _LON)


def test_is_daytime_false_at_night() -> None:
    assert not is_daytime(datetime(2026, 5, 22, 1, 0, tzinfo=UTC), _LAT, _LON)
