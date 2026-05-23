from __future__ import annotations

from datetime import UTC, datetime

from energy_orchestrator.config.models import ChargerControlConfig
from energy_orchestrator.decision.charger_control import (
    ATTACHED_CHARGEABLE_STATUS,
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


def test_attached_chargeable_status_set() -> None:
    # Plugged/Charging/Suspended (1-4) plus this firmware's Finishing (5) and
    # Reserved (6) count as a car present; Available (0) / Unavailable (7) /
    # Faulted (8) do not.
    assert {1, 2, 3, 4, 5, 6} <= ATTACHED_CHARGEABLE_STATUS
    assert ATTACHED_CHARGEABLE_STATUS.isdisjoint({0, 7, 8})


def test_adopt_manual_target_clamps_to_envelope() -> None:
    ctrl = ChargerController(_config())  # max 16
    assert ctrl.adopt_manual_target(10.0) == 10.0
    assert ctrl.target_a == 10.0  # integral accumulator now reflects the manual value
    assert ctrl.adopt_manual_target(99.0) == 16.0  # clamped to max_charge_a
    assert ctrl.adopt_manual_target(-5.0) == 0.0  # clamped to 0


def test_pauses_below_battery_soc_floor() -> None:
    ctrl = ChargerController(_config())  # floor 30, hysteresis 3
    cmd = ctrl.decide(_inputs(battery_soc_pct=25.0))
    assert cmd.paused


def test_soc_floor_hysteresis() -> None:
    ctrl = ChargerController(_config())  # floor 30, hyst 3 -> re-enable at 33
    # Strong export keeps the resume signal covered regardless of the SoC-taper,
    # so this isolates the SoC-floor latch.
    strong = {"grid_power_w": -5000.0, "battery_power_w": 0.0}
    assert not ctrl.decide(_inputs(battery_soc_pct=50.0, **strong)).paused
    # Drop just below the floor -> disabled.
    assert ctrl.decide(_inputs(battery_soc_pct=29.0, **strong)).paused
    # Back between floor and floor+hysteresis -> still disabled (latched).
    assert ctrl.decide(_inputs(battery_soc_pct=31.0, **strong)).paused
    # Above floor + hysteresis -> re-enabled.
    assert not ctrl.decide(_inputs(battery_soc_pct=34.0, **strong)).paused


# ----- resume from pause -------------------------------------------------------


def test_resume_to_min_when_signal_covers_it() -> None:
    ctrl = ChargerController(_config())  # resume threshold 4300, min 6
    # Battery charging 5 kW with no export -> that charge power is diverted to
    # the car -> signal over the resume threshold.
    cmd = ctrl.decide(_inputs(grid_power_w=0.0, battery_power_w=-5000.0))
    assert not cmd.paused and cmd.target_a == 6.0


def test_resume_blocked_when_signal_too_low() -> None:
    ctrl = ChargerController(_config())
    # SoC just above the floor -> taper reserve tiny (~640 W); a modest 3000 W
    # export still leaves the signal under the 4300 resume threshold.
    cmd = ctrl.decide(_inputs(grid_power_w=-3000.0, battery_power_w=0.0, battery_soc_pct=35.0))
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
    # export 600 + the SoC-taper reserve (battery idle at 50%) clears the 500 W
    # export threshold, and the car is drawing near the command -> +1 A.
    cmd = ctrl.decide(_inputs(grid_power_w=-600.0, battery_power_w=0.0, actual_current_a=10.0))
    assert not cmd.paused and cmd.target_a == 11.0


def test_downtick_when_battery_over_discharging_beyond_cap() -> None:
    # Big house load: the battery discharges far past its SoC-tapered cap to
    # cover it while grid import stays ~0 (the battery, not the grid, absorbs the
    # deficit). The car is over-drawing on the battery, so it down-ticks even
    # though measured grid import is below the threshold.
    ctrl = ChargerController(_config())  # cap(50%) = 9000*(50-30)/70 ~= 2571 W
    ctrl._target_a = 8.0
    cmd = ctrl.decide(
        _inputs(
            grid_power_w=58.0,  # ~0 grid — battery covers the load
            battery_power_w=6034.0,  # discharging ~3.5 kW past the cap
            battery_soc_pct=50.0,
            actual_current_a=7.4,
        )
    )
    assert not cmd.paused and cmd.target_a == 7.0  # -1 A despite import < threshold


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
    # SoC just above the floor -> tiny taper reserve (~390 W), no export, no
    # import: signal sits in the dead-band (<= 500 W export) -> hold.
    cmd = ctrl.decide(
        _inputs(grid_power_w=0.0, battery_power_w=0.0, battery_soc_pct=33.0, actual_current_a=10.0)
    )
    assert cmd.target_a == 10.0


# ----- available-power signal: battery reserve ---------------------------------


def test_battery_reserve_enables_charging_without_export() -> None:
    ctrl = ChargerController(_config())
    # High SoC, battery idle, no grid export: the SoC-tapered reserve alone
    # (~5.1 kW at 70%) clears the resume threshold.
    cmd = ctrl.decide(_inputs(grid_power_w=0.0, battery_power_w=0.0, battery_soc_pct=70.0))
    assert not cmd.paused and cmd.target_a == 6.0


def test_discharge_reserve_below_resume_pauses() -> None:
    ctrl = ChargerController(_config())
    # Battery discharging at mid SoC, no export: the tapered reserve (~2.6 kW at
    # 50%) stays under the 4300 W resume threshold -> can't start.
    cmd = ctrl.decide(_inputs(grid_power_w=0.0, battery_power_w=4000.0, battery_soc_pct=50.0))
    assert cmd.paused


def test_discharge_reserve_self_limits_at_tapered_cap() -> None:
    # Regression for the diverging up-tick: at 51% SoC the tapered cap is
    # 9000*(51-30)/70 = 2700 W. With the battery already discharging 2700 W to
    # feed the car, the remaining headroom is 0, so the signal is in the
    # dead-band and the setpoint HOLDS instead of up-ticking the battery to death.
    ctrl = ChargerController(_config())
    ctrl._target_a = 10.0
    cmd = ctrl.decide(
        _inputs(
            grid_power_w=0.0, battery_power_w=2700.0, battery_soc_pct=51.0, actual_current_a=10.0
        )
    )
    assert cmd.target_a == 10.0


def test_discharge_reserve_upticks_below_cap() -> None:
    # Same 2700 W cap at 51% SoC, but the battery is only discharging 500 W ->
    # ~2200 W of headroom remains -> still room to ramp -> +1 A.
    ctrl = ChargerController(_config())
    ctrl._target_a = 10.0
    cmd = ctrl.decide(
        _inputs(
            grid_power_w=0.0, battery_power_w=500.0, battery_soc_pct=51.0, actual_current_a=10.0
        )
    )
    assert cmd.target_a == 11.0


def test_taper_floor_decoupled_from_charge_stop_floor() -> None:
    # The taper depends on taper_floor_soc_pct, not the charge-stop floor. At
    # 33% SoC (above the unchanged 30% gate) the default taper floor (30) gives
    # a tiny ~390 W reserve -> dead-band hold; a taper floor of 10 gives
    # 9000*(33-10)/90 ~= 2300 W -> enough to up-tick. battery_floor_soc_pct stays
    # 30, so the eligibility gate is identical in both cases.
    args = {
        "grid_power_w": 0.0,
        "battery_power_w": 0.0,
        "battery_soc_pct": 33.0,
        "actual_current_a": 10.0,
    }
    held = ChargerController(_config())  # taper floor 30 (default)
    held._target_a = 10.0
    assert held.decide(_inputs(**args)).target_a == 10.0  # dead-band hold

    aggressive = ChargerController(_config(taper_floor_soc_pct=10.0))
    aggressive._target_a = 10.0
    assert aggressive.decide(_inputs(**args)).target_a == 11.0  # bigger reserve -> up-tick


def test_charging_battery_still_taps_taper_reserve() -> None:
    # Unified signal: while the battery is charging only a trickle at a healthy
    # SoC, the car can still lean on the SoC-tapered reserve and up-tick. (The
    # old charge/discharge split would have held on the tiny charge power alone.)
    ctrl = ChargerController(_config())  # taper floor 30
    ctrl._target_a = 6.0
    # 52% SoC, battery charging 193 W, no export, car drawing ~6 A:
    # cap(52) = 9000*(52-30)/70 ~= 2829; reserve = max(0, 2829 - (-193)) ~= 3022
    # -> well over the 500 W export threshold -> up-tick.
    cmd = ctrl.decide(
        _inputs(
            grid_power_w=0.0, battery_power_w=-193.0, battery_soc_pct=52.0, actual_current_a=6.0
        )
    )
    assert not cmd.paused and cmd.target_a == 7.0


# ----- daytime helper ----------------------------------------------------------


def test_is_daytime_true_at_local_noon() -> None:
    assert is_daytime(datetime(2026, 5, 22, 12, 0, tzinfo=UTC), _LAT, _LON)


def test_is_daytime_false_at_night() -> None:
    assert not is_daytime(datetime(2026, 5, 22, 1, 0, tzinfo=UTC), _LAT, _LON)
