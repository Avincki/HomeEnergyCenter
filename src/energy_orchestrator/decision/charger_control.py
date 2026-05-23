"""Rule-based control of the Etrel EV charger during solar daytime.

This is a *second* decision domain alongside the SolarEdge inverter engine —
it has its own inputs (grid flow, battery power/SoC, charger status), its own
output (a charging-current setpoint or pause), and its own state (an integral
accumulator). It is deliberately kept separate from the inverter rule chain
rather than bolted onto ``DecisionState`` (ON/OFF), which cannot express a
continuous charge current.

Strategy (solar daytime only — night rules are a later phase):

* Below ``battery_floor_soc_pct`` the home battery has priority — don't charge.
* Above it, follow available power. The available-power signal is the measured
  grid export **plus** a battery term that depends on the battery's direction:
  while it's *charging*, its charge power is added (diverting surplus into the
  car instead of the battery); while *discharging or idle*, the car may lean on
  the battery up to an SoC-tapered cap (full at 100% SoC, linearly to 0 at the
  floor) **minus what the battery is already discharging**, so the term is the
  remaining headroom and shrinks as the car drains the battery (self-limiting). This is what
  makes 3-phase charging (6 A ≈ 4.1 kW minimum) actually engage — pure solar
  export rarely clears that floor.
* Up-tick the setpoint when the signal exceeds ``export_threshold_w``; down-tick
  when *measured* grid import exceeds ``import_threshold_w``. The down-tick is on
  real import (not the virtual signal) and takes priority, so a wrong reserve
  estimate or a battery sitting at its floor can never push us into a sustained
  grid import.
* The charge envelope is {0 (pause)} or [min_charge_a..max_charge_a]; below the
  minimum the charger pauses, and resumes only when the signal covers that
  minimum draw (so resuming doesn't immediately import and flap).

Anti-windup: while charging, the setpoint is only raised when the car is
actually drawing near the commanded current — otherwise an unplugged / full /
externally-clamped car would wind the accumulator to the ceiling and then dump
full current the moment it could draw.

Every threshold here is a config knob (``ChargerControlConfig``) — they are
expected to be tuned empirically during the live-test window, not in code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from astral import Observer
from astral.sun import elevation, sun

from energy_orchestrator.config.models import ChargerControlConfig

logger = structlog.stdlib.get_logger(__name__)

# Etrel connector-status codes that mean "a car is plugged in and chargeable".
# Permissive on purpose: this Sonnen-managed firmware parks a plugged-in, idle
# car in 6=Reserved (not 1=Plugged) and cycles through 5=Finishing, so both are
# included (confirmed against the live unit 2026-05-23 — without 6 the car sat
# stuck "paused: no chargeable car attached"). Only 0=Available (no car),
# 7=Unavailable and 8=Faulted are treated as "no chargeable car". Status
# reliability on this firmware is suspect, so the anti-windup guard below is the
# real safety net — a connector with no car actually drawing current won't ramp
# up regardless of the reported status.
ATTACHED_CHARGEABLE_STATUS: frozenset[int] = frozenset({1, 2, 3, 4, 5, 6})

# How closely the measured draw must track the commanded current to count as
# "really charging" for the anti-windup guard (amps). Wide enough to tolerate
# the few-second pilot ramp after a step, narrow enough to catch a car that
# isn't drawing at all.
_DRAW_TRACKING_TOLERANCE_A = 2.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def is_daytime(when: datetime, latitude: float, longitude: float) -> bool:
    """True when ``when`` (UTC) is between sunrise and sunset at the site.

    Uses absolute (UTC) sun times — display timezone is irrelevant here; an
    instant is daytime or not regardless of how it's later rendered. Handles
    polar day/night (where sunrise/sunset don't exist) by falling back to the
    sun's elevation.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    observer = Observer(latitude=latitude, longitude=longitude)
    try:
        events = sun(observer, date=when.date(), tzinfo=UTC)
    except ValueError:
        # No sunrise/sunset that day (polar) — daytime iff the sun is up.
        return elevation(observer, when) > 0
    return events["sunrise"] <= when <= events["sunset"]


@dataclass(frozen=True)
class ChargerInputs:
    """One tick's inputs for the charger controller.

    ``grid_power_w`` is the P1 meter's active power (+ = importing, - = exporting).
    ``battery_power_w`` is Sonnen ``Pac_total_W`` (+ = discharging, - = charging).
    ``actual_current_a`` is the charger's measured L1 current (anti-windup); may
    be ``None`` if that read failed this tick.
    """

    timestamp: datetime
    is_daytime: bool
    car_attached: bool
    actual_current_a: float | None
    battery_soc_pct: float
    grid_power_w: float
    battery_power_w: float


@dataclass(frozen=True)
class ChargerCommand:
    """Controller output. ``target_a`` is the desired current; ``paused`` means
    apply 0 A (target below the minimum). ``reason`` is for logs/audit."""

    target_a: float
    paused: bool
    reason: str


class ChargerController:
    """Stateful integral controller for the charging-current setpoint.

    Holds the accumulator (``_target_a``) and the SoC-floor hysteresis latch
    across ticks. ``decide`` is otherwise a pure function of its inputs and that
    state, so it is straightforward to unit-test tick-by-tick.
    """

    def __init__(self, config: ChargerControlConfig) -> None:
        self.config = config
        # Integral accumulator (amps). 0 means paused; charging lives in
        # [min_charge_a, max_charge_a]. Survives across ticks; a process restart
        # resets it to 0 (safe — the controller ramps back up).
        self._target_a: float = 0.0
        # Hysteresis latch for the battery-SoC floor: once disabled by a low
        # SoC we only re-enable above floor + hysteresis, and vice-versa.
        self._soc_enabled: bool = False

    @property
    def target_a(self) -> float:
        return self._target_a

    def decide(self, inp: ChargerInputs) -> ChargerCommand:
        cfg = self.config

        # ----- eligibility gates: any failure parks the car (reset to 0) -----
        if not inp.is_daytime:
            return self._pause("outside solar daytime (no night rules yet)")
        if not inp.car_attached:
            return self._pause("no chargeable car attached")
        if not self._soc_floor_ok(inp.battery_soc_pct):
            return self._pause(
                f"battery SoC {inp.battery_soc_pct:.0f}% below floor "
                f"{cfg.battery_floor_soc_pct:.0f}%"
            )

        # ----- eligible: available-power signal -----
        # signal = measured grid export + a battery term chosen by what the home
        # battery is doing:
        #   * Charging (battery_power_w < 0): the power flowing INTO the battery
        #     is surplus we'd rather divert to the car, so add it whole. As the
        #     car ramps it diverts that power, charging_w shrinks, self-limiting.
        #   * Discharging / idle: let the car lean on the battery up to an
        #     SoC-tapered cap (full battery_max_output_w at 100% SoC, linearly to
        #     0 at the floor) MINUS what the battery is already discharging, so
        #     the term is the *remaining* headroom. Subtracting the discharge is
        #     essential: without it the (SoC-fixed) cap stays constant while the
        #     car drains the battery, the grid never imports, and the setpoint
        #     up-ticks without bound (this restores the self-limiting feedback the
        #     original max_output - discharge reserve had).
        # The down-tick on *measured* grid import is still the backstop against a
        # wrong estimate driving sustained import.
        export_w = max(0.0, -inp.grid_power_w)
        import_w = max(0.0, inp.grid_power_w)
        charging_w = max(0.0, -inp.battery_power_w)
        discharge_w = max(0.0, inp.battery_power_w)
        if charging_w > 0.0:
            reserve_w = charging_w
        else:
            soc_span = max(1.0, 100.0 - cfg.battery_floor_soc_pct)
            tapered_cap = _clamp(
                cfg.battery_max_output_w
                * (inp.battery_soc_pct - cfg.battery_floor_soc_pct)
                / soc_span,
                0.0,
                cfg.battery_max_output_w,
            )
            reserve_w = _clamp(tapered_cap - discharge_w, 0.0, tapered_cap)
        signal_w = export_w + reserve_w

        if self._target_a < cfg.min_charge_a:
            return self._maybe_resume(import_w, signal_w)
        return self._track(import_w, signal_w, inp.actual_current_a)

    # ----- helpers -----------------------------------------------------------

    def _soc_floor_ok(self, soc_pct: float) -> bool:
        cfg = self.config
        if self._soc_enabled:
            self._soc_enabled = soc_pct >= cfg.battery_floor_soc_pct
        else:
            self._soc_enabled = (
                soc_pct >= cfg.battery_floor_soc_pct + cfg.battery_floor_hysteresis_pct
            )
        return self._soc_enabled

    def _pause(self, reason: str) -> ChargerCommand:
        self._target_a = 0.0
        return ChargerCommand(target_a=0.0, paused=True, reason=reason)

    def _maybe_resume(self, import_w: float, signal_w: float) -> ChargerCommand:
        cfg = self.config
        # Measured import wins over the virtual signal — never resume into a
        # real grid draw (e.g. battery actually at its floor).
        if import_w > cfg.import_threshold_w:
            return ChargerCommand(0.0, True, f"paused: importing {import_w:.0f} W")
        if signal_w >= cfg.resume_surplus_threshold_w:
            self._target_a = cfg.min_charge_a
            return ChargerCommand(
                cfg.min_charge_a,
                False,
                f"resume at {cfg.min_charge_a:.0f} A (signal {signal_w:.0f} W)",
            )
        return ChargerCommand(
            0.0,
            True,
            f"paused: signal {signal_w:.0f} W < resume {cfg.resume_surplus_threshold_w:.0f} W",
        )

    def _track(self, import_w: float, signal_w: float, actual_a: float | None) -> ChargerCommand:
        cfg = self.config
        target = self._target_a
        if import_w > cfg.import_threshold_w:
            target -= cfg.step_a
            reason = f"import {import_w:.0f} W > {cfg.import_threshold_w:.0f} W"
        elif signal_w > cfg.export_threshold_w and self._may_increase(actual_a):
            target += cfg.step_a
            reason = f"signal {signal_w:.0f} W > {cfg.export_threshold_w:.0f} W"
        elif signal_w > cfg.export_threshold_w:
            reason = f"surplus {signal_w:.0f} W but car not drawing — hold (anti-windup)"
        else:
            reason = f"signal {signal_w:.0f} W within dead-band — hold"

        target = _clamp(target, 0.0, cfg.max_charge_a)
        if target < cfg.min_charge_a:
            self._target_a = 0.0
            return ChargerCommand(
                0.0, True, f"{reason} -> below {cfg.min_charge_a:.0f} A min, pause"
            )
        self._target_a = target
        return ChargerCommand(target, False, f"{reason} -> {target:.0f} A")

    def _may_increase(self, actual_a: float | None) -> bool:
        # Anti-windup: only ramp up if the car is actually drawing near the
        # commanded current. A failed current read (None) is treated as "allow"
        # so a transient read gap doesn't stall the ramp.
        if actual_a is None:
            return True
        return actual_a >= self._target_a - _DRAW_TRACKING_TOLERANCE_A
