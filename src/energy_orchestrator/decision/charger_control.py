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

Start-failure backoff: repeatedly offering then withdrawing the AC pilot can
latch an EV's onboard charger into a protective fault (observed 2026-05-24 on the
EQS — it then failed on the Tesla wall box too and only a DC fast-charge cleared
it). So if the car doesn't begin drawing within ``start_timeout_s`` of a resume
(or stops drawing mid-session), the controller pauses and refuses to resume for a
cooldown (``failed_start_cooldown_s``, escalating to ``backoff_cooldown_s`` after
``max_consecutive_failed_starts``) instead of re-offering on the next tick. An
ordinary down-tick-to-pause also holds for ``resume_cooldown_s`` so a marginal
surplus can't flap the charger on/off tick-to-tick. The backoff resets on a real
draw or when the car detaches (a fresh plug-in session).

Every threshold here is a config knob (``ChargerControlConfig``) — they are
expected to be tuned empirically during the live-test window, not in code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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

# Measured L1 current (amps) at/above which the car counts as "actually drawing"
# — used by the start-failure backoff to tell a real charging session from a
# commanded-but-idle one. Same magnitude as the orchestrator kick-start's
# drawing threshold.
_MIN_DRAWING_CURRENT_A = 2.0


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
        # --- start-failure backoff state (see _track / _maybe_resume) ----------
        # When the current "commanding charge but not yet confirmed drawing"
        # stretch began (UTC). None when paused. Slid forward on every drawing
        # tick, so it measures *continuous* not-drawing time while commanded.
        self._offer_since: datetime | None = None
        # Don't resume before this instant (set after a pause). None == no cooldown.
        self._cooldown_until: datetime | None = None
        # Consecutive failed starts this plug-in session; resets on a real draw or
        # when the car detaches, and escalates the cooldown once it hits the
        # configured max.
        self._consecutive_failed_starts: int = 0

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
        # A manual Send is an explicit user action — clear any start-failure
        # backoff so it isn't immediately blocked. The offer timer re-arms on the
        # next decide tick, so a manually-commanded car that still won't draw
        # backs off rather than being cycled forever.
        self._reset_start_tracking()
        return self._target_a

    def decide(self, inp: ChargerInputs) -> ChargerCommand:
        cfg = self.config
        now = inp.timestamp

        # ----- eligibility gates: any failure parks the car (reset to 0) -----
        if not inp.is_daytime:
            return self._pause("outside solar daytime (no night rules yet)")
        if not inp.car_attached:
            # A detached connector is a fresh plug-in next time — drop any
            # start-failure backoff so a new session starts from a clean slate.
            self._reset_start_tracking()
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
            return self._maybe_resume(now, import_w, signal_w)
        if self._offer_since is None:
            # Commanding a charge with no offer timer running (e.g. a manual Send
            # adopted the target) — stamp it now so the start-failure timeout
            # applies to manually-commanded sessions too.
            self._offer_since = now
        return self._track(now, import_w, signal_w, over_discharge_w, inp.actual_current_a)

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
        self._offer_since = None
        return ChargerCommand(target_a=0.0, paused=True, reason=reason)

    def _pause_with_cooldown(self, now: datetime, seconds: float, reason: str) -> ChargerCommand:
        """Pause and refuse to resume for ``seconds`` (anti-flap / anti-fault)."""
        self._target_a = 0.0
        self._offer_since = None
        self._cooldown_until = now + timedelta(seconds=seconds)
        return ChargerCommand(target_a=0.0, paused=True, reason=reason)

    def _reset_start_tracking(self) -> None:
        self._offer_since = None
        self._cooldown_until = None
        self._consecutive_failed_starts = 0

    def _fail_start(self, now: datetime) -> ChargerCommand:
        """A commanded car never drew (or stopped) — pause with an escalating
        cooldown so we don't keep cycling the AC pilot and risk faulting it."""
        cfg = self.config
        self._consecutive_failed_starts += 1
        n = self._consecutive_failed_starts
        if n >= cfg.max_consecutive_failed_starts:
            cooldown = cfg.backoff_cooldown_s
            note = f"backing off {cooldown:.0f}s after {n} failed starts"
        else:
            cooldown = cfg.failed_start_cooldown_s
            note = (
                f"cooldown {cooldown:.0f}s "
                f"(failed start {n}/{cfg.max_consecutive_failed_starts})"
            )
        return self._pause_with_cooldown(
            now,
            cooldown,
            f"car not drawing {cfg.start_timeout_s:.0f}s after command — {note}",
        )

    @staticmethod
    def _is_drawing(actual_a: float | None) -> bool:
        return actual_a is not None and actual_a >= _MIN_DRAWING_CURRENT_A

    def _maybe_resume(self, now: datetime, import_w: float, signal_w: float) -> ChargerCommand:
        cfg = self.config
        # Measured import wins over the virtual signal — never resume into a
        # real grid draw (e.g. battery actually at its floor).
        if import_w > cfg.import_threshold_w:
            return ChargerCommand(0.0, True, f"paused: importing {import_w:.0f} W")
        # Hold off resuming while a start-failure / anti-flap cooldown is active —
        # this is what stops a refusing car from being cycled tick after tick.
        if self._cooldown_until is not None and now < self._cooldown_until:
            remaining = (self._cooldown_until - now).total_seconds()
            return ChargerCommand(
                0.0,
                True,
                f"paused: cooldown {remaining:.0f}s remaining "
                f"({self._consecutive_failed_starts} failed start(s))",
            )
        if signal_w >= cfg.resume_surplus_threshold_w:
            self._target_a = cfg.min_charge_a
            self._offer_since = now
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
        now: datetime,
        import_w: float,
        signal_w: float,
        over_discharge_w: float,
        actual_a: float | None,
    ) -> ChargerCommand:
        cfg = self.config
        # Start-failure backoff: track continuous not-drawing time while commanded.
        # A confirmed draw slides the timer and clears the failure count; a
        # confirmed not-drawing stretch past start_timeout_s means the car isn't
        # taking the offer (never started, or finished / at its limit) -> back off
        # instead of cycling the pilot. A missing current read (None) is neither
        # confirmation, so it neither slides the timer nor trips the backoff.
        if self._is_drawing(actual_a):
            self._offer_since = now
            self._consecutive_failed_starts = 0
        elif (
            actual_a is not None
            and self._offer_since is not None
            and (now - self._offer_since).total_seconds() >= cfg.start_timeout_s
        ):
            return self._fail_start(now)

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
            # Hold the pause for resume_cooldown_s so a marginal surplus can't
            # flap the charger on and off tick-to-tick.
            return self._pause_with_cooldown(
                now,
                cfg.resume_cooldown_s,
                f"{reason} -> below {cfg.min_charge_a:.0f} A min, pause",
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
