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

from energy_orchestrator.config.models import AppConfig
from energy_orchestrator.data import UnitOfWork
from energy_orchestrator.data.models import (
    Decision,
    DecisionState,
    Reading,
    SourceName,
)
from energy_orchestrator.decision import DecisionEngine, TickContext
from energy_orchestrator.decision.forecast import get_current_hour_price
from energy_orchestrator.devices import (
    DeviceClient,
    DeviceError,
    DeviceReading,
    SolarEdgeClient,
    create_device_client,
)
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
from energy_orchestrator.web.override import OverrideController

logger = structlog.stdlib.get_logger(__name__)

# SolarEdge active-power-limit values.
_OFF_PCT = 0
_ON_PCT = 100

# Price-fetch window: yesterday + today + tomorrow (UTC). We pull yesterday
# too because the dashboard renders prices on a local-time x-axis: in any
# timezone east of UTC, the first hours of "today, local" map to UTC slots
# in yesterday's date, so without those we'd leave a gap at the start of
# the chart. Three days covers any TZ within ±24 h.
_PRICE_PAST_DAYS = timedelta(days=1)
_PRICE_FUTURE_DAYS = timedelta(days=2)


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

        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # Gates the decision/actuation block — None means "fire on the very
        # next tick", which gives us a decision immediately on startup rather
        # than waiting a full decision_interval_s.
        self._last_decision_at: datetime | None = None

    # ----- lifecycle ----------------------------------------------------------

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
        # nested helpers) carries the same timestamp.
        with structlog.contextvars.bound_contextvars(tick_at=when.isoformat()):
            sonnen_r, car_r, p1_r, small_r, _solar_r, large_r, etrel_r = await asyncio.gather(
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

                    if record.state_changed:
                        logger.info(
                            "decision state changed",
                            state=record.state.value,
                            rule=record.rule_fired,
                            manual_override=record.manual_override,
                        )

                    if not self.config.decision.dry_run and record.state_changed:
                        await self._actuate_solaredge(record.state)

            await self._persist(reading, decision)

    # ----- helpers ------------------------------------------------------------

    def _should_decide(self, when: datetime) -> bool:
        """True on the first tick or when at least ``decision_interval_s``
        has elapsed since the last decision actually fired."""
        if self._last_decision_at is None:
            return True
        elapsed = (when - self._last_decision_at).total_seconds()
        return elapsed >= self.config.decision_interval_s

    async def _read_optional(
        self, client: DeviceClient[Any] | None
    ) -> DeviceReading | None:
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
        rows = [
            (p.timestamp, p.consumption_eur_per_kwh, p.injection_eur_per_kwh)
            for p in points
        ]
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
            etrel_power_w=(
                _as_float(etrel.data.get("power_w")) if etrel is not None else None
            ),
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


__all__ = ["TickLoop"]
