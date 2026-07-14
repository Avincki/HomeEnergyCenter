"""Rule-based control of the Etrel EV charger during solar daytime.

This is a *second* decision domain alongside the SolarEdge inverter engine —
it has its own inputs (grid flow, battery power/SoC, charger status), its own
output (a charging-current setpoint or pause), and its own state (an integral
accumulator). It is deliberately kept separate from the inverter rule chain
rather than bolted onto ``DecisionState`` (ON/OFF), which cannot express a
continuous charge current.

Strategy (solar daytime; a separate fixed-current night mode is described at
the end):

* Below ``battery_floor_soc_pct`` the home battery has priority — don't charge.
* Above it, follow available power. The available-power signal is the measured
  grid export **plus** the battery's headroom for the car, computed in one shot
  regardless of charge/discharge direction as ``max(0, tapered_cap -
  battery_power)`` (battery_power signed: + discharging, - charging). So while
  the battery is charging the term is ``cap + charge`` (the car may take the
  incoming surplus *and* lean on the battery), and while discharging it's
  ``cap - discharge`` (shrinking to 0 at the cap). The cap tapers from
  battery_max_output_w at 100% SoC to 0 at ``taper_floor_soc_pct`` (separate from
  the charge-stop ``battery_floor_soc_pct``). Self-limiting either way — as the
  car ramps and the battery swings toward discharging the cap, the term falls to
  0; the measured-import down-tick is the backstop. This is what
  makes 3-phase charging (6 A ≈ 4.1 kW minimum) actually engage — pure solar
  export rarely clears that floor.
* Up-tick the setpoint when the signal exceeds ``export_threshold_w``. Down-tick
  on either of two conditions: *measured* grid import over ``import_threshold_w``
  (the hard backstop against a wrong estimate driving real import — takes
  priority), or the battery discharging more than ``import_threshold_w`` **past**
  its tapered cap (a big house load that the battery, not the grid, is covering —
  import stays ~0 yet the battery drains hard). The over-discharge down-tick
  mirrors the up-tick, so the setpoint settles where the battery discharges ~= the
  tapered cap.
* The charge envelope is {0 (pause)} or [min_charge_a..max_charge_a]; below the
  minimum the charger pauses, and resumes only when the signal covers that
  minimum draw (so resuming doesn't immediately import and flap).

Anti-windup: while charging, the setpoint is only raised when the car is
actually drawing near the commanded current — otherwise an unplugged / full /
externally-clamped car would wind the accumulator to the ceiling and then dump
full current the moment it could draw.

Night mode (``night_charge_enabled``): from the later of sunset and
``night_start_time`` (Brussels wall-clock, default 24:00 = midnight) the car
charges at a fixed ``night_charge_a`` fed from the home battery, down to
``night_floor_soc_pct``
(a deliberately lower floor than the daytime charge-stop — the bet is that
tomorrow's solar refills the battery). There is no surplus to follow at night,
so the envelope is binary {0, night_charge_a} and the guard is *measured* grid
import: importing past ``import_threshold_w`` pauses the charge instead of
buying power. Resuming needs the battery to have headroom for house + car
(estimated from its discharge while the car is idle) AND a cooldown after an
import-pause — both matter because the EQS latches an AC-charging fault when
the offer rapid-cycles, so a mis-tuned ``battery_max_output_w`` must degrade
to a slow retry, never a per-tick flap.

Every threshold here is a config knob (``ChargerControlConfig``) — they are
expected to be tuned empirically during the live-test window, not in code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import structlog
from astral import Observer
from astral.sun import elevation, sun

from energy_orchestrator.config.models import ChargerControlConfig
from energy_orchestrator.utils.clock import to_local

logger = structlog.stdlib.get_logger(__name__)


class ChargerMode(StrEnum):
    """Runtime control mode for the charger, toggled from the Etrel tile.

    Lives in memory on the tick loop (not config) — it is a transient operator
    choice, reset to ``OPTIMIZED`` on a process restart, mirroring how the
    inverter override is not persisted.

    * ``OPTIMIZED`` — the rule engine (:class:`ChargerController`) decides the
      setpoint from solar surplus / battery headroom.
    * ``FORCED`` — hold an operator-set current regardless of solar, daytime,
      or battery SoC; the only limit kept is the 16 A installation cap. The
      controller's ``decide`` is suppressed; the setpoint is still defended
      against Sonnen clamping by the kick-start (steadily, never cycled).
    """

    OPTIMIZED = "optimized"
    FORCED = "forced"


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

# Per-amp draw of a 3-phase charge (3 x 230 V) — used at night to estimate the
# car's draw before offering it, since there is no surplus signal to lean on.
_W_PER_AMP_3PHASE = 690.0

# After a night charge is paused for grid import, don't retry before this has
# elapsed. The headroom estimate below is the primary anti-flap gate; this
# cooldown is the backstop for a mis-tuned battery_max_output_w, bounding the
# worst case to ~2 offer-cycles/hour (the EQS latches an AC fault when the
# offer rapid-cycles).
_NIGHT_IMPORT_RETRY = timedelta(minutes=30)


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
        # Night-mode state: its own SoC latch (the night floor is lower than
        # the daytime one) and the earliest instant a night charge may resume
        # after an import-pause. Stale values are harmless across day/night
        # transitions — both are only read inside the night branch.
        self._night_soc_enabled: bool = False
        self._night_resume_at: datetime | None = None

    @property
    def target_a(self) -> float:
        return self._target_a

    def adopt_manual_target(self, amps: float) -> float:
        """Adopt a manually-commanded current as the controller's target.

        While charger control is active the rule engine re-asserts its own
        target every decision tick, so a one-off manual write (the dashboard
        "Send" button / the API) gets overwritten within a tick unless the
        controller takes it as its new starting point. This sets the integral
        accumulator to the clamped manual value so the controller continues
        from there (it may still ramp up/down on the next tick as conditions
        warrant) instead of fighting it. Returns the clamped value applied.
        """
        self._target_a = _clamp(amps, 0.0, self.config.max_charge_a)
        return self._target_a

    def decide(self, inp: ChargerInputs) -> ChargerCommand:
        cfg = self.config

        # ----- eligibility gates: any failure parks the car (reset to 0) -----
        if not inp.is_daytime:
            return self._night_decide(inp)
        if not inp.car_attached:
            return self._pause("no chargeable car attached")
        if not self._soc_floor_ok(inp.battery_soc_pct):
            return self._pause(
                f"battery SoC {inp.battery_soc_pct:.0f}% below floor "
                f"{cfg.battery_floor_soc_pct:.0f}%"
            )

        # ----- eligible: available-power signal -----
        # signal = measured grid export + the battery's headroom for the car,
        # computed in one shot regardless of charge/discharge direction:
        #   reserve = max(0, tapered_cap - battery_power_w)
        # battery_power_w is signed (+ discharging, - charging), so:
        #   * Charging  -> cap + |charge|: the car may take the incoming surplus
        #     AND lean on the battery up to the tapered cap.
        #   * Discharging -> cap - discharge: shrinks to 0 once the battery is
        #     already discharging the whole cap.
        # Self-limiting either way: as the car ramps and the battery swings toward
        # discharging the cap, the term falls to 0. The cap tapers from
        # battery_max_output_w at 100% SoC to 0 at taper_floor_soc_pct. The
        # down-tick on *measured* grid import is the backstop against a wrong
        # estimate driving sustained import.
        export_w = max(0.0, -inp.grid_power_w)
        import_w = max(0.0, inp.grid_power_w)
        soc_span = max(1.0, 100.0 - cfg.taper_floor_soc_pct)
        tapered_cap = _clamp(
            cfg.battery_max_output_w * (inp.battery_soc_pct - cfg.taper_floor_soc_pct) / soc_span,
            0.0,
            cfg.battery_max_output_w,
        )
        reserve_w = max(0.0, tapered_cap - inp.battery_power_w)
        signal_w = export_w + reserve_w
        # Mirror of the reserve for the down-tick: how far the battery is
        # discharging *beyond* its tapered cap. The grid-import down-tick can't
        # see this — when a big house load is covered by the battery (not the
        # grid), import stays ~0 while the battery drains hard — so the car backs
        # off on over-discharge too. Symmetric with the up-tick, so the setpoint
        # settles where the battery discharges ~= the tapered cap.
        over_discharge_w = max(0.0, inp.battery_power_w - tapered_cap)

        if self._target_a < cfg.min_charge_a:
            return self._maybe_resume(import_w, signal_w)
        return self._track(import_w, signal_w, over_discharge_w, inp.actual_current_a)

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

    def _track(
        self,
        import_w: float,
        signal_w: float,
        over_discharge_w: float,
        actual_a: float | None,
    ) -> ChargerCommand:
        cfg = self.config
        target = self._target_a
        if import_w > cfg.import_threshold_w:
            target -= cfg.step_a
            reason = f"import {import_w:.0f} W > {cfg.import_threshold_w:.0f} W"
        elif over_discharge_w > cfg.import_threshold_w:
            target -= cfg.step_a
            reason = (
                f"battery over-discharging {over_discharge_w:.0f} W past tapered "
                f"cap > {cfg.import_threshold_w:.0f} W"
            )
        elif signal_w > cfg.export_threshold_w and self._may_increase(actual_a):
            target += cfg.step_a
            reason = f"signal {signal_w:.0f} W > {cfg.export_threshold_w:.0f} W"
        elif signal_w > cfg.export_threshold_w:
            reason = (
                f"surplus {signal_w:.0f} W > {cfg.export_threshold_w:.0f} W but car "
                f"not drawing — hold (anti-windup)"
            )
        else:
            # Show every trigger power so the hold is self-explanatory: the up-tick
            # threshold the signal is under, and the down-tick threshold the
            # import / over-discharge are under.
            reason = (
                f"signal {signal_w:.0f} W (up-tick >{cfg.export_threshold_w:.0f} W), "
                f"import {import_w:.0f} W / over-discharge {over_discharge_w:.0f} W "
                f"(down-tick >{cfg.import_threshold_w:.0f} W) — within dead-band, hold"
            )

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

    # ----- night mode ---------------------------------------------------------

    def _night_decide(self, inp: ChargerInputs) -> ChargerCommand:
        """Fixed-current battery-drain charge after sunset.

        Binary envelope {0, night_charge_a}: there is no surplus to follow, so
        the only in-charge guard is measured grid import (pause, never buy).
        Resume needs battery headroom for house + car plus the import-pause
        cooldown — see the module docstring for why both.
        """
        cfg = self.config
        if not cfg.night_charge_enabled:
            return self._pause("outside solar daytime (night charging disabled)")
        if not self._night_start_reached(inp.timestamp):
            return self._pause(f"night: waiting for start time {cfg.night_start_time} (Brussels)")
        if not inp.car_attached:
            return self._pause("no chargeable car attached")
        if not self._night_soc_floor_ok(inp.battery_soc_pct):
            return self._pause(
                f"night: battery SoC {inp.battery_soc_pct:.0f}% at floor "
                f"{cfg.night_floor_soc_pct:.0f}% — done for tonight"
            )

        import_w = max(0.0, inp.grid_power_w)
        night_a = _clamp(cfg.night_charge_a, cfg.min_charge_a, cfg.max_charge_a)
        if self._target_a >= cfg.min_charge_a:
            return self._night_hold(inp, import_w, night_a)
        return self._night_maybe_start(inp, import_w, night_a)

    def _night_hold(self, inp: ChargerInputs, import_w: float, night_a: float) -> ChargerCommand:
        """Charging (or handing over from a daytime ramp at sunset — the target
        just steps to night_charge_a, no pause cycle in between)."""
        cfg = self.config
        if import_w > cfg.import_threshold_w:
            self._night_resume_at = inp.timestamp + _NIGHT_IMPORT_RETRY
            return self._pause(
                f"night: importing {import_w:.0f} W > "
                f"{cfg.import_threshold_w:.0f} W — pause instead of buying"
            )
        self._target_a = night_a
        return ChargerCommand(
            night_a, False, f"night: hold {night_a:.0f} A (import {import_w:.0f} W)"
        )

    def _night_maybe_start(
        self, inp: ChargerInputs, import_w: float, night_a: float
    ) -> ChargerCommand:
        cfg = self.config
        if self._night_resume_at is not None and inp.timestamp < self._night_resume_at:
            return ChargerCommand(
                0.0,
                True,
                "night: cooling down after import-pause "
                f"(retry from {self._night_resume_at:%H:%M} UTC)",
            )
        if import_w > cfg.import_threshold_w:
            return ChargerCommand(
                0.0, True, f"night: already importing {import_w:.0f} W — won't add the car"
            )
        # With the car idle, house load ~= battery discharge + import (no solar
        # at night). Offer the charge only if the battery could carry both.
        house_w = max(0.0, inp.battery_power_w) + import_w
        draw_w = night_a * _W_PER_AMP_3PHASE
        if house_w + draw_w > cfg.battery_max_output_w:
            return ChargerCommand(
                0.0,
                True,
                f"night: house {house_w:.0f} W + car {draw_w:.0f} W would exceed "
                f"battery max {cfg.battery_max_output_w:.0f} W",
            )
        self._night_resume_at = None
        self._target_a = night_a
        return ChargerCommand(
            night_a,
            False,
            f"night: start {night_a:.0f} A from battery "
            f"(SoC {inp.battery_soc_pct:.0f}% > floor {cfg.night_floor_soc_pct:.0f}%)",
        )

    def _night_start_reached(self, when: datetime) -> bool:
        """True once the local wall clock has passed ``night_start_time``.

        The configured time is Brussels wall-clock (24:00 = midnight). Both
        instants are mapped to minutes-since-noon so the sunset -> sunrise
        window is contiguous across midnight: 22:00 < 24:00 < 03:00 order
        correctly, and the pre-dawn hours always count as past an evening
        start time.
        """
        local = to_local(when)
        now_m = (local.hour * 60 + local.minute - 12 * 60) % (24 * 60)
        start_m = (self.config.night_start_minutes - 12 * 60) % (24 * 60)
        return now_m >= start_m

    def _night_soc_floor_ok(self, soc_pct: float) -> bool:
        # Same latch shape as the daytime floor, against the (lower) night
        # floor. At night the SoC only falls, so once the floor trips the
        # charge stays off until morning; the hysteresis matters only for
        # measurement jitter right at the boundary.
        cfg = self.config
        if self._night_soc_enabled:
            self._night_soc_enabled = soc_pct > cfg.night_floor_soc_pct
        else:
            self._night_soc_enabled = (
                soc_pct > cfg.night_floor_soc_pct + cfg.battery_floor_hysteresis_pct
            )
        return self._night_soc_enabled
