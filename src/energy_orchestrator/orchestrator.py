"""Background tick loop.

Every ``poll_interval_s`` the loop:
  1. Reads all five devices in parallel (sonnen, car charger, P1, small solar,
     SolarEdge limit register).
  2. Refreshes the price cache from the configured provider when stale.
  3. Persists a ``Reading`` row capturing whatever data made it through.

Once per ``decision_interval_s`` (gated on the loop's own clock) the same tick
also:
  4. If battery SoC is available, builds a ``TickContext`` and runs the
     decision engine; persists the resulting ``Decision`` row.
  5. If ``decision.dry_run`` is false and the state changed, actuates the
     SolarEdge active-power-limit register (0 % for OFF, 100 % for ON).

That decoupling lets the dashboard see fresh ``Reading`` rows at the poll
cadence while keeping decision evaluation (and any inverter writes) on a
slower, less-chatty cadence.

Per-source success/error is recorded against ``SourceStatus`` so the debug
board's health panel reflects what the loop is actually seeing.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from energy_orchestrator.config.models import AppConfig, ChargerControlConfig
from energy_orchestrator.data import UnitOfWork
from energy_orchestrator.data.models import (
    Decision,
    DecisionState,
    Reading,
    SourceName,
)
from energy_orchestrator.decision import DecisionEngine, TickContext
from energy_orchestrator.decision.charger_control import (
    ATTACHED_CHARGEABLE_STATUS,
    ChargerCommand,
    ChargerController,
    ChargerInputs,
    is_daytime,
)
from energy_orchestrator.decision.forecast import get_current_hour_price
from energy_orchestrator.devices import (
    DeviceClient,
    DeviceError,
    DeviceReading,
    SolarEdgeClient,
    create_device_client,
)
from energy_orchestrator.devices.etrel import EtrelInchClient
from energy_orchestrator.prices import (
    PriceCache,
    PriceError,
    PricePoint,
    PriceProvider,
    create_price_provider,
)
from energy_orchestrator.solar import (
    ForecastSolarProvider,
    SolarCache,
    SolarError,
    SolarForecast,
    SolarProvider,
)
from energy_orchestrator.utils.clock import to_local
from energy_orchestrator.web.override import OverrideController

logger = structlog.stdlib.get_logger(__name__)

# SolarEdge active-power-limit values.
_OFF_PCT = 0
_ON_PCT = 100

# Charger setpoint self-heal noise floor (amps): only re-write when the live
# write-side setpoint differs from the desired value by more than this, so a
# steady command re-asserts at most once instead of every tick.
_CHARGER_SETPOINT_NOISE_A = 0.1

# Kick-start: when we command a charge current but the EV / Sonnen clamps the
# active setpoint to ~0 at startup (the connector bounces Reserved <-> Suspended
# and never draws), a single steady write loses that fight — only repeated
# re-writes get the session to latch (this is what mashing the manual "Send"
# button does). We re-assert the setpoint on the poll cadence while a session is
# stalled, and stop the instant real current flows.
_CHARGER_KICK_DRAWING_A = 2.0  # measured L1 current at/above which the car is
# drawing -> session latched -> stop kicking.
_CHARGER_KICK_MAX_S = 180.0  # give up re-asserting after this long per episode
# so a genuinely-declining car isn't poked forever.

# Price-fetch window: yesterday + today + tomorrow (UTC). We pull yesterday
# too because the dashboard renders prices on a local-time x-axis: in any
# timezone east of UTC, the first hours of "today, local" map to UTC slots
# in yesterday's date, so without those we'd leave a gap at the start of
# the chart. Three days covers any TZ within ±24 h.
_PRICE_PAST_DAYS = timedelta(days=1)
_PRICE_FUTURE_DAYS = timedelta(days=2)

# Daily cadence for history pruning. Without this, readings/decisions accrue
# forever — the repositories all expose ``prune()`` but nothing was calling
# them, so ``storage.history_retention_days`` was effectively ignored.
_PRUNE_INTERVAL = timedelta(hours=24)


class TickLoop:
    """Owns the device clients, price provider, and price cache, and drives
    one orchestration tick per ``poll_interval_s``.

    The constructor builds clients from config; ``start()`` schedules the
    background task; ``stop()`` cancels it and closes every client.
    """

    def __init__(
        self,
        config: AppConfig,
        session_factory: async_sessionmaker[AsyncSession],
        override_controller: OverrideController,
        price_cache: PriceCache,
        solar_cache: SolarCache,
    ) -> None:
        self.config = config
        self._session_factory = session_factory
        self._override_controller = override_controller
        self._price_cache = price_cache
        self._solar_cache = solar_cache

        self._sonnen = create_device_client(config.sonnen)
        self._car_charger = create_device_client(config.homewizard.car_charger)
        self._p1_meter = create_device_client(config.homewizard.p1_meter)
        self._small_solar = create_device_client(config.homewizard.small_solar)
        # Optional second HomeWizard kWh meter — None when the user has no
        # ``homewizard.large_solar`` section in config.
        self._large_solar: DeviceClient[Any] | None = (
            create_device_client(config.homewizard.large_solar)
            if config.homewizard.large_solar is not None
            else None
        )
        # Optional Etrel INCH EV charger over Modbus TCP. The HomeWizard
        # car-charger meter measures Tesla + Etrel together; reading Etrel
        # power here lets the dashboard split per-vehicle draw.
        self._etrel: DeviceClient[Any] | None = (
            create_device_client(config.etrel) if config.etrel is not None else None
        )
        solaredge = create_device_client(config.solaredge)
        if not isinstance(solaredge, SolarEdgeClient):
            raise TypeError(
                "registry returned non-SolarEdgeClient for SolarEdgeConfig: "
                f"{type(solaredge).__name__}"
            )
        self._solaredge: SolarEdgeClient = solaredge
        self._price_provider: PriceProvider = create_price_provider(config.prices)
        self._solar_provider: SolarProvider | None = (
            ForecastSolarProvider(config.solar) if config.solar is not None else None
        )
        self._engine = DecisionEngine(config.decision)
        # Optional charger rule-control (separate decision domain from the
        # inverter engine). Needs the Etrel device (the actuator) and the solar
        # config (lat/lon for the sunrise/sunset daytime gate). Inert otherwise.
        self._charger: ChargerController | None = (
            ChargerController(config.charger_control)
            if (
                config.charger_control.enabled
                and config.etrel is not None
                and config.solar is not None
            )
            else None
        )
        if config.charger_control.enabled and self._charger is None:
            logger.warning(
                "charger_control.enabled but inactive: needs both an 'etrel' "
                "device and 'solar' (lat/lon for sunrise/sunset) configured"
            )
        # Latest charger decision, exposed to the dashboard via /api/state. None
        # until the first decision tick (or when charger control is inactive).
        self._charger_status: dict[str, Any] | None = None
        # Kick-start episode tracking (see _kick_charger_if_stalled): when the
        # current stalled-start episode began and whether we've logged giving up
        # on it. Both reset when the stall clears.
        self._charger_kick_started_at: datetime | None = None
        self._charger_kick_gave_up: bool = False

        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # Gates the decision/actuation block — None means "fire on the very
        # next tick", which gives us a decision immediately on startup rather
        # than waiting a full decision_interval_s.
        self._last_decision_at: datetime | None = None
        # Gates the history-prune step. None means "fire on the next tick";
        # subsequent prunes are gated by ``_PRUNE_INTERVAL``.
        self._last_prune_at: datetime | None = None

    # ----- lifecycle ----------------------------------------------------------

    @property
    def etrel_client(self) -> EtrelInchClient | None:
        """Public accessor for the Etrel client owned by this loop.

        Routing API writes through the same client (rather than spawning a
        fresh one) avoids opening a second Modbus TCP connection to the
        charger; that firmware silently drops PDUs on the second connection
        even when the TCP handshake passes. The client's internal lock
        serializes the read tick against ad-hoc writes.
        """
        if self._etrel is None:
            return None
        if not isinstance(self._etrel, EtrelInchClient):
            return None
        return self._etrel

    @property
    def charger_status(self) -> dict[str, Any] | None:
        """Latest charger-control decision, for the dashboard tile.

        ``None`` when charger control is inactive or hasn't decided yet.
        """
        return self._charger_status

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("TickLoop already started")
        self._task = asyncio.create_task(self._run(), name="energy-orchestrator-tick-loop")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._close_resources()

    async def _close_resources(self) -> None:
        clients: list[DeviceClient[Any]] = [
            self._sonnen,
            self._car_charger,
            self._p1_meter,
            self._small_solar,
            self._solaredge,
        ]
        if self._large_solar is not None:
            clients.append(self._large_solar)
        if self._etrel is not None:
            clients.append(self._etrel)
        for client in clients:
            with contextlib.suppress(Exception):
                await client.close()
        with contextlib.suppress(Exception):
            await self._price_provider.close()
        if self._solar_provider is not None:
            with contextlib.suppress(Exception):
                await self._solar_provider.close()

    async def _run(self) -> None:
        # First tick happens immediately; subsequent ticks honour the interval.
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # the loop must keep running
                logger.exception("tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.poll_interval_s,
                )
            except TimeoutError:
                continue

    # ----- single tick --------------------------------------------------------

    async def tick(self, *, now: datetime | None = None) -> None:
        """Run one orchestration cycle. Used by the background loop and tests."""
        when = now or datetime.now(UTC)

        # Bind tick_at so every log line emitted during this tick (including
        # nested helpers) carries the same timestamp — rendered in local
        # (Brussels) time to match the rest of the user-facing surface.
        with structlog.contextvars.bound_contextvars(tick_at=to_local(when).isoformat()):
            sonnen_r, car_r, p1_r, small_r, solar_r, large_r, etrel_r = await asyncio.gather(
                self._read_one(self._sonnen),
                self._read_one(self._car_charger),
                self._read_one(self._p1_meter),
                self._read_one(self._small_solar),
                self._read_one(self._solaredge),
                self._read_optional(self._large_solar),
                self._read_optional(self._etrel),
            )

            await self._refresh_prices_if_stale(when)
            await self._refresh_solar_if_stale(when)
            current_price = get_current_hour_price(self._price_cache.points(), when)

            reading = self._build_reading(
                when, sonnen_r, car_r, p1_r, small_r, large_r, etrel_r, current_price
            )
            decision: Decision | None = None

            if self._should_decide(when):
                soc = sonnen_r.data.get("soc_pct") if sonnen_r is not None else None
                if soc is None:
                    # Skip the decision step — spec says missing essential data
                    # must not reach the engine. We still persist the partial
                    # reading so the debug board reflects what we did manage to
                    # read. Don't advance _last_decision_at — we want to retry
                    # on the next poll, not wait a full decision_interval_s.
                    logger.warning("tick skipped decision: sonnen SoC unavailable")
                else:
                    previous_state = await self._fetch_previous_state()
                    ctx = TickContext(
                        timestamp=when,
                        battery_soc_pct=float(soc),
                        car_is_charging=self._car_is_charging(car_r),
                        small_solar_w=self._small_solar_w(small_r),
                        prices=self._price_cache.points(),
                        previous_state=previous_state,
                        battery_capacity_kwh=self.config.sonnen.capacity_kwh,
                        override=self._override_controller.get_active(when),
                    )
                    record = self._engine.decide(ctx)
                    decision = Decision(
                        timestamp=record.timestamp,
                        state=record.state.value,
                        rule_fired=record.rule_fired,
                        reason=record.reason,
                        state_changed=record.state_changed,
                        manual_override=record.manual_override,
                        override_mode=(
                            record.override_mode.value if record.override_mode is not None else None
                        ),
                        forecast_end_soc_pct=record.forecast_end_soc_pct,
                    )
                    self._last_decision_at = when

                    # Descriptive per-decision log, mirroring the charger-control
                    # line in _run_charger_control so both decision domains leave
                    # a "what rule fired and why" trail in the log on every tick,
                    # not only on state changes. state_changed flags the ON/OFF
                    # transitions for anyone grepping just the moves.
                    logger.info(
                        "solaredge decision",
                        state=record.state.value,
                        rule=record.rule_fired,
                        reason=record.reason,
                        state_changed=record.state_changed,
                        manual_override=record.manual_override,
                        forecast_end_soc_pct=record.forecast_end_soc_pct,
                    )

                    if not self.config.decision.dry_run and self._needs_actuation(
                        record.state, solar_r
                    ):
                        await self._actuate_solaredge(record.state)

                # Charger control is a separate decision domain — it runs on the
                # same cadence but with its own inputs and skips itself cleanly
                # when essential readings are missing (so a SoC gap that skips
                # the inverter decision doesn't block it from a safe pause).
                if self._charger is not None:
                    await self._run_charger_control(when, sonnen_r, p1_r, etrel_r)

            # Kick-start a stalled charge on every poll (not only the 60 s
            # decision tick): the EV/Sonnen can clamp our setpoint to 0 at
            # startup, and only repeated re-writes get the session to latch.
            await self._kick_charger_if_stalled(when, etrel_r)

            await self._persist(reading, decision)

            if self._should_prune(when):
                await self._prune_history()
                self._last_prune_at = when

    # ----- helpers ------------------------------------------------------------

    def _should_prune(self, when: datetime) -> bool:
        if self._last_prune_at is None:
            return True
        return (when - self._last_prune_at) >= _PRUNE_INTERVAL

    async def _prune_history(self) -> None:
        """Delete readings / decisions / price points / solar forecast rows
        older than ``storage.history_retention_days``.

        Wrapped in a single UoW so the four DELETEs land atomically. Failure
        is logged and swallowed — pruning is bookkeeping, not load-bearing,
        and must not be allowed to kill the tick loop.
        """
        days = self.config.storage.history_retention_days
        try:
            async with UnitOfWork(self._session_factory) as uow:
                readings = await uow.readings.prune(days)
                decisions = await uow.decisions.prune(days)
                prices = await uow.price_points.prune(days)
                solar = await uow.solar_forecast.prune(days)
                await uow.commit()
        except Exception:
            logger.exception("history prune failed")
            return
        logger.info(
            "history pruned",
            retention_days=days,
            readings_deleted=readings,
            decisions_deleted=decisions,
            price_points_deleted=prices,
            solar_forecast_points_deleted=solar,
        )

    def _should_decide(self, when: datetime) -> bool:
        """True on the first tick or when at least ``decision_interval_s``
        has elapsed since the last decision actually fired."""
        if self._last_decision_at is None:
            return True
        elapsed = (when - self._last_decision_at).total_seconds()
        return elapsed >= self.config.decision_interval_s

    async def _read_optional(self, client: DeviceClient[Any] | None) -> DeviceReading | None:
        """Same as _read_one but no-ops if the client is unconfigured."""
        if client is None:
            return None
        return await self._read_one(client)

    async def _read_one(self, client: DeviceClient[Any]) -> DeviceReading | None:
        try:
            reading = await client.read_data()
        except DeviceError as e:
            await self._record_status_error(client.source_name, str(e))
            return None
        except Exception as e:  # defensive: don't kill the tick
            logger.exception("device read unexpected error", source=client.source_name.value)
            await self._record_status_error(client.source_name, f"unexpected: {e}")
            return None

        payload = dict(reading.data) if reading is not None else None
        await self._record_status_success(client.source_name, payload)
        return reading

    async def _refresh_prices_if_stale(self, now: datetime) -> None:
        if not self._price_cache.is_stale(now):
            return
        today_utc_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = today_utc_midnight - _PRICE_PAST_DAYS
        end = today_utc_midnight + _PRICE_FUTURE_DAYS
        try:
            points = await self._price_provider.fetch_prices(start, end)
        except PriceError as e:
            await self._record_status_error(SourceName.PRICES, str(e))
            return
        except Exception as e:  # defensive
            logger.exception("unexpected error fetching prices")
            await self._record_status_error(SourceName.PRICES, f"unexpected: {e}")
            return
        self._price_cache.replace(points, now)
        await self._persist_prices(points)
        await self._record_status_success(SourceName.PRICES, {"hours": len(points)})

    async def _refresh_solar_if_stale(self, now: datetime) -> None:
        if self._solar_provider is None:
            return
        if not self._solar_cache.is_stale(now):
            return
        try:
            forecast = await self._solar_provider.fetch_forecast()
        except SolarError as e:
            # Cooldown the cache so we don't retry every poll — Forecast.Solar
            # rate-limits per IP and a failed fetch (esp. 429) means the
            # bucket is empty until the hourly reset.
            self._solar_cache.mark_failed(now)
            await self._record_status_error(SourceName.SOLAR_FORECAST, str(e))
            return
        except Exception as e:  # defensive
            self._solar_cache.mark_failed(now)
            logger.exception("unexpected error fetching solar forecast")
            await self._record_status_error(SourceName.SOLAR_FORECAST, f"unexpected: {e}")
            return
        self._solar_cache.replace(forecast, now)
        await self._persist_solar_forecast(forecast)
        await self._record_status_success(
            SourceName.SOLAR_FORECAST,
            {
                "points": len(forecast.points),
                "watt_hours_today": forecast.watt_hours_today,
            },
        )

    async def _persist_prices(self, points: Sequence[PricePoint]) -> None:
        if not points:
            return
        rows = [(p.timestamp, p.consumption_eur_per_kwh, p.injection_eur_per_kwh) for p in points]
        try:
            async with UnitOfWork(self._session_factory) as uow:
                await uow.price_points.upsert_many(rows)
                await uow.commit()
        except Exception:  # never let history bookkeeping kill the tick
            logger.exception("price-points persistence failed")

    async def _persist_solar_forecast(self, forecast: SolarForecast) -> None:
        if not forecast.per_plane and not forecast.points:
            return
        # Prefer the per-plane breakdown so historic days can be summed back
        # together on read; fall back to a synthetic ``_total`` plane when a
        # provider only returns the aggregate.
        if forecast.per_plane:
            per_plane = {
                name: [(p.timestamp, p.watts) for p in series]
                for name, series in forecast.per_plane.items()
            }
        else:
            per_plane = {
                "_total": [(p.timestamp, p.watts) for p in forecast.points],
            }
        try:
            async with UnitOfWork(self._session_factory) as uow:
                await uow.solar_forecast.upsert_per_plane(per_plane)
                await uow.commit()
        except Exception:  # never let history bookkeeping kill the tick
            logger.exception("solar-forecast persistence failed")

    def _needs_actuation(self, desired: DecisionState, solar_reading: DeviceReading | None) -> bool:
        """True if the inverter's actual active-power-limit register differs
        from the value implied by ``desired``.

        Driving actuation off the live read instead of ``record.state_changed``
        makes the loop self-healing against drift between the persisted decision
        history and the hardware. The override→auto transition is the canonical
        case: while a force-off is active, every persisted decision row is OFF,
        so when the override clears, comparing the new decision against the
        last persisted state can yield ``state_changed=False`` even though the
        inverter is still pinned at 0 %. Reading the register directly closes
        that loop. When the read failed this tick, fall back to actuating
        unconditionally — better an extra write than a missed one.
        """
        target = _ON_PCT if desired is DecisionState.ON else _OFF_PCT
        if solar_reading is None:
            return True
        actual = solar_reading.data.get("active_power_limit_pct")
        if actual is None:
            return True
        return int(actual) != target

    async def _actuate_solaredge(self, state: DecisionState) -> None:
        target = _ON_PCT if state is DecisionState.ON else _OFF_PCT
        try:
            await self._solaredge.set_active_power_limit(target)
        except DeviceError as e:
            await self._record_status_error(SourceName.SOLAREDGE, f"actuation failed: {e}")
            return
        except Exception as e:  # defensive
            logger.exception("unexpected error actuating SolarEdge")
            await self._record_status_error(SourceName.SOLAREDGE, f"actuation unexpected: {e}")
            return
        await self._record_status_success(
            SourceName.SOLAREDGE, {"active_power_limit_pct": target, "actuated": True}
        )

    async def _run_charger_control(
        self,
        when: datetime,
        sonnen_r: DeviceReading | None,
        p1_r: DeviceReading | None,
        etrel_r: DeviceReading | None,
    ) -> None:
        """Evaluate the charger controller and (unless dry-run) actuate the Etrel.

        Skips the tick if any essential input is missing rather than forcing a
        pause on a transient read gap — the self-healing re-assert next tick
        catches up. SoC, battery power, grid power and charger status are
        essential; the measured charge current is anti-windup only and may be
        absent.
        """
        if self._charger is None or self.config.solar is None:
            return
        soc = sonnen_r.data.get("soc_pct") if sonnen_r is not None else None
        batt = sonnen_r.data.get("battery_power_w") if sonnen_r is not None else None
        grid = p1_r.data.get("active_power_w") if p1_r is not None else None
        status = etrel_r.data.get("status_code") if etrel_r is not None else None
        if soc is None or batt is None or grid is None or status is None:
            logger.warning(
                "charger control skipped: incomplete inputs",
                have_soc=soc is not None,
                have_battery_power=batt is not None,
                have_grid=grid is not None,
                have_status=status is not None,
            )
            return
        inputs = ChargerInputs(
            timestamp=when,
            is_daytime=self._is_daytime(when),
            car_attached=int(status) in ATTACHED_CHARGEABLE_STATUS,
            actual_current_a=(
                _as_float(etrel_r.data.get("current_l1_a")) if etrel_r is not None else None
            ),
            battery_soc_pct=float(soc),
            grid_power_w=float(grid),
            battery_power_w=float(batt),
        )
        command = self._charger.decide(inputs)
        self._charger_status = {
            "timestamp": when.isoformat(),
            "target_a": command.target_a,
            "paused": command.paused,
            "reason": command.reason,
            "dry_run": self.config.charger_control.dry_run,
        }
        logger.info(
            "charger control decision",
            target_a=command.target_a,
            paused=command.paused,
            reason=command.reason,
            dry_run=self.config.charger_control.dry_run,
            grid_w=inputs.grid_power_w,
            battery_w=inputs.battery_power_w,
            soc_pct=inputs.battery_soc_pct,
            daytime=inputs.is_daytime,
            attached=inputs.car_attached,
        )
        if self.config.charger_control.dry_run:
            return
        await self._actuate_charger(command, etrel_r)

    async def _actuate_charger(
        self, command: ChargerCommand, etrel_r: DeviceReading | None
    ) -> None:
        """Write the charger setpoint, self-healing against drift.

        Writes only when the live write-side setpoint (holding reg 8 readback)
        differs from the desired value beyond the noise floor, so a steady
        command re-asserts at most once while an externally-clamped setpoint is
        corrected each tick. The 16 A installation cap is enforced in the device.
        Actuation outcome is logged, not recorded on ``SourceStatus`` — that
        would clobber the Etrel telemetry payload the dashboard tile reads.
        """
        client = self.etrel_client
        if client is None:
            return
        desired = command.target_a  # 0.0 == pause
        live = etrel_r.data.get("set_current_a") if etrel_r is not None else None
        if live is not None and abs(float(live) - desired) <= _CHARGER_SETPOINT_NOISE_A:
            return
        try:
            await client.set_charging_current_a(desired)
        except DeviceError as e:
            logger.warning("charger actuation failed", target_a=desired, error=str(e))
            return
        except Exception:  # defensive — never let actuation kill the tick
            logger.exception("unexpected error actuating charger")
            return
        logger.info("charger actuated", target_a=desired, paused=command.paused)

    async def _kick_charger_if_stalled(self, when: datetime, etrel_r: DeviceReading | None) -> None:
        """Re-assert the charge setpoint on the poll cadence when a session stalls.

        At startup the EV / Sonnen cluster channel can clamp the active setpoint
        to ~0 right after we command a current (the connector bounces
        Reserved <-> Suspended and never draws). A single steady write loses that
        fight; repeated re-writes win it — which is what mashing the manual
        "Send" button does. While we're commanding a charge current
        (``target >= min``) but the car isn't drawing and the active setpoint is
        clamped below our command, re-write the setpoint every poll to nudge the
        session into latching, then stop the instant real current flows. Bounded
        to ``_CHARGER_KICK_MAX_S`` per episode so a genuinely-declining car
        (scheduled-off, at its limit) isn't poked forever; the window resets when
        the stall clears.
        """
        if self._charger is None or self.config.charger_control.dry_run:
            return
        desired = self._charger.target_a
        active = _as_float(etrel_r.data.get("setpoint_a")) if etrel_r is not None else None
        current = _as_float(etrel_r.data.get("current_l1_a")) if etrel_r is not None else None
        if not _charger_kick_stalled(
            desired_a=desired,
            active_a=active,
            current_a=current,
            min_charge_a=self.config.charger_control.min_charge_a,
        ):
            self._charger_kick_started_at = None
            self._charger_kick_gave_up = False
            return
        if self._charger_kick_started_at is None:
            self._charger_kick_started_at = when
        elapsed = (when - self._charger_kick_started_at).total_seconds()
        if elapsed > _CHARGER_KICK_MAX_S:
            if not self._charger_kick_gave_up:
                self._charger_kick_gave_up = True
                logger.warning(
                    "charger kick-start gave up — car still not drawing",
                    target_a=desired,
                    active_a=active,
                    current_a=current,
                    elapsed_s=round(elapsed, 1),
                )
            return
        client = self.etrel_client
        if client is None:
            return
        try:
            await client.set_charging_current_a(desired)
        except DeviceError as e:
            logger.warning("charger kick-start write failed", target_a=desired, error=str(e))
            return
        except Exception:  # defensive — never let a kick write kill the tick
            logger.exception("unexpected error kicking charger")
            return
        logger.info(
            "charger kick-start re-asserted",
            target_a=desired,
            active_a=active,
            current_a=current,
            elapsed_s=round(elapsed, 1),
        )

    def apply_hot_config(self, new_config: AppConfig) -> list[str]:
        """Apply a freshly-saved config to the running loop WITHOUT a restart.

        Hot-swaps the tuning/decision config that components read live each tick
        (charger-control thresholds, decision bands, solar calibration, price
        factors, intervals), and invalidates the solar/price caches when their
        config changed so calibration / factors re-apply on the next tick rather
        than after the normal refresh window.

        Device connections (host/port/timeout/token/api_version), the price
        provider identity + api_key, and web/storage/log paths are built once at
        startup and are NOT reconnected here. The list of changed
        restart-required sections is returned (and logged) so callers can tell
        the user what still needs a restart.

        Synchronous and await-free, so it runs atomically against the tick loop
        on the same event loop. The web ``/config`` save calls this after
        persisting ``config.yaml``.
        """
        old = self.config
        needs_restart = _connection_fields_changed(old, new_config)
        if needs_restart:
            logger.warning(
                "config saved; these sections need an app restart to take effect",
                sections=needs_restart,
            )
        # Swap the live config so direct ``self.config`` reads see new values.
        self.config = new_config
        # Components cache their own slice and read it live — point them at the
        # new one (no rebuild, so any in-flight state is preserved).
        self._engine.config = new_config.decision
        if self._solar_provider is not None and new_config.solar is not None:
            self._solar_provider.config = new_config.solar
        # Price factors are read at fetch; only swap when the provider class +
        # credentials are unchanged (an identity change is flagged for restart).
        if (
            old.prices.provider == new_config.prices.provider
            and old.prices.api_key == new_config.prices.api_key
        ):
            self._price_provider.config = new_config.prices
        self._apply_charger_config(new_config.charger_control)
        # Force the calibrated/factored caches to refetch so new values land on
        # the next tick instead of after the refresh window (Option A).
        if new_config.solar != old.solar:
            self._solar_cache.invalidate()
        if new_config.prices != old.prices:
            self._price_cache.invalidate()
        logger.info("config hot-reloaded (no restart)", needs_restart=needs_restart)
        return needs_restart

    def _apply_charger_config(self, cc: ChargerControlConfig) -> None:
        """Apply charger-control changes live, preserving controller state.

        Updates the active controller's thresholds in place (keeps the integral
        ramp + SoC latch) and handles enable/disable transitions. (Re)building a
        controller needs the Etrel device + solar provider, which are created at
        startup — a newly-added device still needs a restart.
        """
        if self._charger is not None:
            if cc.enabled:
                self._charger.config = cc
            else:
                self._charger = None
                self._charger_status = None
                self._charger_kick_started_at = None
                self._charger_kick_gave_up = False
        elif cc.enabled and self._etrel is not None and self._solar_provider is not None:
            self._charger = ChargerController(cc)

    def adopt_manual_charger_target(self, amps: float) -> bool:
        """Push a manual current command into the running charger controller.

        Called by the /etrel/set-current API after a manual write so the
        controller adopts the value as its target instead of overwriting it on
        the next decision tick. Returns True when charger control is active (and
        so adopted it), False when it's inactive (the manual write stands alone).
        """
        if self._charger is None:
            return False
        applied = self._charger.adopt_manual_target(amps)
        logger.info("charger manual target adopted", amps=applied)
        return True

    def _is_daytime(self, when: datetime) -> bool:
        solar = self.config.solar
        if solar is None:
            return False
        return is_daytime(when, solar.latitude, solar.longitude)

    async def _fetch_previous_state(self) -> DecisionState | None:
        async with UnitOfWork(self._session_factory) as uow:
            latest = await uow.decisions.latest()
        if latest is None:
            return None
        try:
            return DecisionState(latest.state)
        except ValueError:
            return None

    async def _persist(self, reading: Reading, decision: Decision | None) -> None:
        async with UnitOfWork(self._session_factory) as uow:
            await uow.readings.add(reading)
            if decision is not None:
                await uow.decisions.add(decision)
            await uow.commit()

    async def _record_status_success(
        self, source: SourceName, payload: dict[str, Any] | None
    ) -> None:
        try:
            async with UnitOfWork(self._session_factory) as uow:
                await uow.source_status.record_success(source.value, payload=payload)
                await uow.commit()
        except Exception:  # never let bookkeeping kill the tick
            logger.exception("source-status success bookkeeping failed", source=source.value)

    async def _record_status_error(self, source: SourceName, message: str) -> None:
        try:
            async with UnitOfWork(self._session_factory) as uow:
                await uow.source_status.record_error(source.value, message=message)
                await uow.commit()
        except Exception:  # never let bookkeeping kill the tick
            logger.exception("source-status error bookkeeping failed", source=source.value)

    # ----- pure functions -----------------------------------------------------

    def _build_reading(
        self,
        when: datetime,
        sonnen: DeviceReading | None,
        car: DeviceReading | None,
        p1: DeviceReading | None,
        small: DeviceReading | None,
        large: DeviceReading | None,
        etrel: DeviceReading | None,
        price: PricePoint | None,
    ) -> Reading:
        sonnen_data = sonnen.data if sonnen is not None else {}
        return Reading(
            timestamp=when,
            battery_soc_pct=_as_float(sonnen_data.get("soc_pct")),
            battery_power_w=_as_float(sonnen_data.get("battery_power_w")),
            house_consumption_w=_as_float(sonnen_data.get("consumption_w")),
            production_w=_as_float(sonnen_data.get("production_w")),
            grid_feed_in_w=_as_float(sonnen_data.get("grid_feed_in_w")),
            car_charger_w=_as_float(car.data.get("active_power_w")) if car is not None else None,
            p1_active_power_w=_as_float(p1.data.get("active_power_w")) if p1 is not None else None,
            small_solar_w=(
                _as_float(small.data.get("active_power_w")) if small is not None else None
            ),
            large_solar_w=(
                _as_float(large.data.get("active_power_w")) if large is not None else None
            ),
            etrel_power_w=(_as_float(etrel.data.get("power_w")) if etrel is not None else None),
            injection_price_eur_per_kwh=(
                price.injection_eur_per_kwh if price is not None else None
            ),
            consumption_price_eur_per_kwh=(
                price.consumption_eur_per_kwh if price is not None else None
            ),
        )

    def _car_is_charging(self, reading: DeviceReading | None) -> bool:
        if reading is None:
            return False
        power = _as_float(reading.data.get("active_power_w"))
        if power is None:
            return False
        return power >= self.config.homewizard.car_charger.charging_threshold_w

    @staticmethod
    def _small_solar_w(reading: DeviceReading | None) -> float:
        if reading is None:
            return 0.0
        v = _as_float(reading.data.get("active_power_w"))
        if v is None:
            return 0.0
        # Magnitude is the production rate; direction is wiring-dependent.
        return abs(v)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _charger_kick_stalled(
    *,
    desired_a: float,
    active_a: float | None,
    current_a: float | None,
    min_charge_a: float,
) -> bool:
    """True when we're commanding a charge current but the session has stalled.

    "Stalled" = the controller wants to charge (``desired_a >= min_charge_a``),
    the car isn't actually drawing (measured current below
    ``_CHARGER_KICK_DRAWING_A``), and the active setpoint is clamped below our
    command (the EV / Sonnen holding it down). Missing telemetry (``None``)
    counts as "not stalled" so we never kick blind.
    """
    if desired_a < min_charge_a:
        return False
    drawing = current_a is not None and current_a >= _CHARGER_KICK_DRAWING_A
    clamped = active_a is not None and active_a < desired_a - _CHARGER_SETPOINT_NOISE_A
    return clamped and not drawing


def _connection_fields_changed(old: AppConfig, new: AppConfig) -> list[str]:
    """Config sections whose change needs an app restart to take effect.

    These build device clients / the price provider / server bindings once at
    startup, so a live config swap can't apply them. Everything else (decision,
    charger_control, solar tuning, price factors, intervals) is hot-reloadable.
    Pydantic models compare field-by-field, so a whole-section ``!=`` catches
    any connection-relevant change (host, port, timeout, token, api_version, …).
    """
    changed: list[str] = []
    if old.sonnen != new.sonnen:
        changed.append("sonnen")
    if old.homewizard != new.homewizard:
        changed.append("homewizard")
    if old.solaredge != new.solaredge:
        changed.append("solaredge")
    if old.etrel != new.etrel:
        changed.append("etrel")
    if old.web != new.web:
        changed.append("web")
    # Pricing: only the provider identity + credentials need a restart; the
    # factors/area are hot (applied at the next fetch).
    if old.prices.provider != new.prices.provider:
        changed.append("prices.provider")
    if old.prices.api_key != new.prices.api_key:
        changed.append("prices.api_key")
    if old.storage.sqlite_path != new.storage.sqlite_path:
        changed.append("storage.sqlite_path")
    if old.logging.log_dir != new.logging.log_dir:
        changed.append("logging.log_dir")
    return changed


__all__ = ["TickLoop"]
