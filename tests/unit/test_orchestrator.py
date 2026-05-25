from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from energy_orchestrator.config.models import (
    AppConfig,
    CarChargerConfig,
    ChargerControlConfig,
    DecisionConfig,
    EtrelInchConfig,
    HomeWizardConfig,
    LoggingConfig,
    P1MeterConfig,
    PricesConfig,
    PricesProvider,
    SmallSolarConfig,
    SolarConfig,
    SolarEdgeConfig,
    SolarPlaneConfig,
    SonnenApiVersion,
    SonnenBatterieConfig,
    StorageConfig,
    WebConfig,
)
from energy_orchestrator.data import (
    Decision,
    DecisionState,
    SourceName,
    UnitOfWork,
    create_engine,
    create_session_factory,
    init_schema,
)
from energy_orchestrator.decision.charger_control import ChargerMode
from energy_orchestrator.devices import DeviceReading
from energy_orchestrator.devices.errors import DeviceConnectionError
from energy_orchestrator.orchestrator import (
    TickLoop,
    _charger_kick_stalled,
    _connection_fields_changed,
)
from energy_orchestrator.prices import PriceCache, PriceFetchError, PricePoint
from energy_orchestrator.solar import SolarCache
from energy_orchestrator.web.override import OverrideController

# ----- shared fixtures + fakes -------------------------------------------------


def _config(tmp_path: Path, *, dry_run: bool = True) -> AppConfig:
    return AppConfig(
        poll_interval_s=30.0,
        sonnen=SonnenBatterieConfig(
            host="192.0.2.10",
            api_version=SonnenApiVersion.V2,
            auth_token="dummy",
            capacity_kwh=10.0,
        ),
        homewizard=HomeWizardConfig(
            car_charger=CarChargerConfig(host="192.0.2.20"),
            p1_meter=P1MeterConfig(host="192.0.2.21"),
            small_solar=SmallSolarConfig(host="192.0.2.22", peak_w=2000.0),
        ),
        solaredge=SolarEdgeConfig(host="192.0.2.30"),
        prices=PricesConfig(provider=PricesProvider.ENTSOE, api_key="dummy"),
        decision=DecisionConfig(dry_run=dry_run),
        storage=StorageConfig(sqlite_path=tmp_path / "tick.db"),
        logging=LoggingConfig(log_dir=tmp_path / "logs"),
        web=WebConfig(),
    )


def _config_with_charger(tmp_path: Path) -> AppConfig:
    """``_config`` plus a configured Etrel + solar so the tick loop builds an
    active ChargerController (model_copy skips re-validation — fine here)."""
    return _config(tmp_path, dry_run=False).model_copy(
        update={
            "etrel": EtrelInchConfig(host="192.0.2.250"),
            "solar": SolarConfig(
                latitude=51.0,
                longitude=3.7,
                planes=(SolarPlaneConfig(name="east", declination=45, azimuth=-90, kwp=6.0),),
            ),
            "charger_control": ChargerControlConfig(enabled=True, dry_run=True),
        }
    )


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(tmp_path / "tick.db")
    await init_schema(engine)
    yield create_session_factory(engine)
    await engine.dispose()


class FakeClient:
    """Stand-in for any DeviceClient — overrides what the loop calls."""

    def __init__(
        self,
        source: SourceName,
        *,
        data: dict[str, Any] | None = None,
        raise_: BaseException | None = None,
    ) -> None:
        self.source_name = source
        self.device_id = source.value
        self._data = data
        self._raise = raise_
        self.read_calls = 0
        self.write_calls: list[int] = []
        self.closed = False

    async def read_data(self) -> DeviceReading | None:
        self.read_calls += 1
        if self._raise is not None:
            raise self._raise
        if self._data is None:
            return None
        return DeviceReading(device_id=self.device_id, data=dict(self._data))

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


class FakeSolarEdge(FakeClient):
    """Adds the actuation surface the tick loop calls in non-dry-run mode."""

    def __init__(
        self,
        *,
        data: dict[str, Any] | None = None,
        raise_on_write: BaseException | None = None,
    ) -> None:
        super().__init__(SourceName.SOLAREDGE, data=data or {"active_power_limit_pct": 100.0})
        self._raise_on_write = raise_on_write

    async def set_active_power_limit(self, value: int) -> None:
        if self._raise_on_write is not None:
            raise self._raise_on_write
        self.write_calls.append(value)
        # Mirror real hardware: the register now reads back what we wrote, so
        # the loop's self-healing _needs_actuation sees actual == target on the
        # next tick and won't re-issue an identical write.
        if self._data is not None:
            self._data["active_power_limit_pct"] = float(value)


class FakeProvider:
    """Stand-in for PriceProvider; orchestrator only calls fetch_prices + close."""

    def __init__(
        self,
        points: Sequence[PricePoint] | None = None,
        *,
        raise_: BaseException | None = None,
    ) -> None:
        self._points = list(points or [])
        self._raise = raise_
        self.fetch_calls = 0
        self.closed = False

    async def fetch_prices(self, start: datetime, end: datetime) -> Sequence[PricePoint]:
        self.fetch_calls += 1
        if self._raise is not None:
            raise self._raise
        return list(self._points)

    async def close(self) -> None:
        self.closed = True


def _install_fakes(
    loop: TickLoop,
    *,
    sonnen: FakeClient,
    car: FakeClient,
    p1: FakeClient,
    small: FakeClient,
    solaredge: FakeSolarEdge,
    provider: FakeProvider,
) -> None:
    loop._sonnen = sonnen  # type: ignore[assignment]
    loop._car_charger = car  # type: ignore[assignment]
    loop._p1_meter = p1  # type: ignore[assignment]
    loop._small_solar = small  # type: ignore[assignment]
    loop._solaredge = solaredge  # type: ignore[assignment]
    loop._price_provider = provider  # type: ignore[assignment]


def _hour_price(when: datetime, *, injection: float, consumption: float = 0.20) -> PricePoint:
    return PricePoint(
        timestamp=when.replace(minute=0, second=0, microsecond=0),
        consumption_eur_per_kwh=consumption,
        injection_eur_per_kwh=injection,
    )


# ----- tests ------------------------------------------------------------------


def test_charger_kick_stalled_detects_clamped_idle() -> None:
    # Commanding 6 A, active setpoint clamped to 0, car not drawing -> stalled.
    assert _charger_kick_stalled(desired_a=6.0, active_a=0.0, current_a=0.0, min_charge_a=6.0)
    # Car actually drawing -> session latched -> not stalled.
    assert not _charger_kick_stalled(desired_a=6.0, active_a=0.0, current_a=6.0, min_charge_a=6.0)
    # Active setpoint matches our command (not clamped) -> not stalled.
    assert not _charger_kick_stalled(desired_a=6.0, active_a=6.0, current_a=0.0, min_charge_a=6.0)
    # Not commanding a charge (paused / below min) -> not stalled.
    assert not _charger_kick_stalled(desired_a=0.0, active_a=0.0, current_a=0.0, min_charge_a=6.0)
    # Missing telemetry -> don't kick blind.
    assert not _charger_kick_stalled(desired_a=6.0, active_a=None, current_a=None, min_charge_a=6.0)


def test_connection_fields_changed(tmp_path: Path) -> None:
    base = _config(tmp_path)
    assert _connection_fields_changed(base, base) == []
    # Device host change -> needs restart.
    sonnen_changed = base.model_copy(
        update={"sonnen": base.sonnen.model_copy(update={"host": "192.0.2.99"})}
    )
    assert "sonnen" in _connection_fields_changed(base, sonnen_changed)
    # Hot tuning changes are NOT flagged.
    decision_changed = base.model_copy(
        update={"decision": base.decision.model_copy(update={"battery_low_soc_pct": 55.0})}
    )
    assert _connection_fields_changed(base, decision_changed) == []
    factor_changed = base.model_copy(
        update={"prices": base.prices.model_copy(update={"injection_factor": 1.5})}
    )
    assert _connection_fields_changed(base, factor_changed) == []


async def test_apply_hot_config_swaps_tuning_and_flags_restart(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = _config(tmp_path)
    price_cache = PriceCache()
    loop = TickLoop(config, session_factory, OverrideController(), price_cache, SolarCache())
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    price_cache.replace([_hour_price(now, injection=0.1)], now)
    assert price_cache.last_refresh is not None

    new = config.model_copy(
        update={
            "decision": config.decision.model_copy(update={"battery_low_soc_pct": 55.0}),
            "prices": config.prices.model_copy(update={"injection_factor": 1.5}),
            "sonnen": config.sonnen.model_copy(update={"host": "192.0.2.99"}),
        }
    )
    restart = loop.apply_hot_config(new)
    assert loop.config is new  # live config swapped
    assert loop._engine.config.battery_low_soc_pct == 55.0  # engine reads it live
    assert "sonnen" in restart  # connection change flagged for restart
    assert price_cache.last_refresh is None  # prices changed -> cache invalidated


async def test_apply_hot_config_swaps_charger_thresholds_in_place(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    loop = TickLoop(
        _config_with_charger(tmp_path),
        session_factory,
        OverrideController(),
        PriceCache(),
        SolarCache(),
    )
    assert loop._charger is not None
    controller = loop._charger
    controller._target_a = 9.0  # ramp state that must survive a hot reload
    new = loop.config.model_copy(
        update={
            "charger_control": loop.config.charger_control.model_copy(update={"min_charge_a": 8.0})
        }
    )
    loop.apply_hot_config(new)
    assert loop._charger is controller  # same instance -> integral state preserved
    assert loop._charger.config.min_charge_a == 8.0
    assert loop._charger._target_a == 9.0


async def test_apply_hot_config_disables_charger_live(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    loop = TickLoop(
        _config_with_charger(tmp_path),
        session_factory,
        OverrideController(),
        PriceCache(),
        SolarCache(),
    )
    assert loop._charger is not None
    new = loop.config.model_copy(
        update={
            "charger_control": loop.config.charger_control.model_copy(update={"enabled": False})
        }
    )
    loop.apply_hot_config(new)
    assert loop._charger is None


async def test_adopt_manual_charger_target(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    loop = TickLoop(
        _config_with_charger(tmp_path),
        session_factory,
        OverrideController(),
        PriceCache(),
        SolarCache(),
    )
    assert loop._charger is not None
    assert loop.adopt_manual_charger_target(11.0) is True
    assert loop._charger.target_a == 11.0


async def test_adopt_manual_charger_target_noop_when_inactive(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    loop = TickLoop(
        _config(tmp_path), session_factory, OverrideController(), PriceCache(), SolarCache()
    )
    assert loop._charger is None
    assert loop.adopt_manual_charger_target(11.0) is False


async def test_set_charger_mode_forced_clamps_and_seeds(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    loop = TickLoop(
        _config_with_charger(tmp_path),
        session_factory,
        OverrideController(),
        PriceCache(),
        SolarCache(),
    )
    assert loop._charger is not None
    # Over the 16 A installation cap -> clamped, and seeded into the controller
    # so the kick-start defends it.
    state = loop.set_charger_mode(ChargerMode.FORCED, 25.0)
    assert state == {"mode": "forced", "forced_amps": 16.0, "active": True}
    assert loop._charger_mode is ChargerMode.FORCED
    assert loop._forced_target_a == 16.0
    assert loop._charger.target_a == 16.0


async def test_set_charger_mode_optimized_clears_forced(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    loop = TickLoop(
        _config_with_charger(tmp_path),
        session_factory,
        OverrideController(),
        PriceCache(),
        SolarCache(),
    )
    loop.set_charger_mode(ChargerMode.FORCED, 10.0)
    state = loop.set_charger_mode(ChargerMode.OPTIMIZED)
    assert state == {"mode": "optimized", "forced_amps": None, "active": True}
    assert loop._charger_mode is ChargerMode.OPTIMIZED
    assert loop._forced_target_a == 0.0


async def test_set_charger_mode_reports_inactive_when_disabled(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    loop = TickLoop(
        _config(tmp_path), session_factory, OverrideController(), PriceCache(), SolarCache()
    )
    assert loop._charger is None
    state = loop.set_charger_mode(ChargerMode.FORCED, 6.0)
    # The mode is recorded but flagged inactive — the tick loop won't act on it.
    assert state["active"] is False
    assert state["mode"] == "forced"


async def test_run_charger_control_forced_sets_status_ignoring_inputs(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """In FORCED the rule engine is bypassed: a forced status is produced even
    when the readings the OPTIMIZED path needs are entirely missing."""
    loop = TickLoop(
        _config_with_charger(tmp_path),
        session_factory,
        OverrideController(),
        PriceCache(),
        SolarCache(),
    )
    loop.set_charger_mode(ChargerMode.FORCED, 12.0)
    # All device readings None — OPTIMIZED would skip with "incomplete inputs".
    await loop._run_charger_control(datetime.now(UTC), None, None, None)
    status = loop.charger_status
    assert status is not None
    assert status["mode"] == "forced"
    assert status["target_a"] == 12.0
    assert status["paused"] is False
    assert "FORCED" in status["reason"]


async def test_disable_charger_control_resets_forced_mode(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    loop = TickLoop(
        _config_with_charger(tmp_path),
        session_factory,
        OverrideController(),
        PriceCache(),
        SolarCache(),
    )
    loop.set_charger_mode(ChargerMode.FORCED, 9.0)
    new = loop.config.model_copy(
        update={
            "charger_control": loop.config.charger_control.model_copy(update={"enabled": False})
        }
    )
    loop.apply_hot_config(new)
    assert loop._charger is None
    assert loop._charger_mode is ChargerMode.OPTIMIZED
    assert loop._forced_target_a == 0.0


async def test_tick_persists_reading_and_decision(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = _config(tmp_path)
    cache = PriceCache()
    loop = TickLoop(config, session_factory, OverrideController(), cache, SolarCache())

    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _install_fakes(
        loop,
        sonnen=FakeClient(
            SourceName.SONNEN,
            data={
                "soc_pct": 75.0,
                "battery_power_w": -200.0,
                "consumption_w": 400.0,
                "production_w": 1500.0,
                "grid_feed_in_w": 1100.0,
            },
        ),
        car=FakeClient(SourceName.CAR_CHARGER, data={"active_power_w": 0.0}),
        p1=FakeClient(SourceName.P1_METER, data={"active_power_w": -800.0}),
        small=FakeClient(SourceName.SMALL_SOLAR, data={"active_power_w": -300.0}),
        solaredge=FakeSolarEdge(data={"active_power_limit_pct": 100.0}),
        provider=FakeProvider([_hour_price(now, injection=0.05)]),
    )

    await loop.tick(now=now)

    async with UnitOfWork(session_factory) as uow:
        latest_reading = await uow.readings.latest()
        latest_decision = await uow.decisions.latest()
        sources = {s.source_name: s for s in await uow.source_status.all()}

    assert latest_reading is not None
    assert latest_reading.battery_soc_pct == 75.0
    assert latest_reading.car_charger_w == 0.0
    assert latest_reading.injection_price_eur_per_kwh == pytest.approx(0.05)
    assert latest_reading.consumption_price_eur_per_kwh == pytest.approx(0.20)
    assert latest_decision is not None
    assert latest_decision.state == DecisionState.ON.value
    assert latest_decision.rule_fired == "positive_injection_price"
    # solar_forecast, large_solar and etrel are conditional on optional config
    # being present — none is configured in this test, so none should appear.
    expected = {s.value for s in SourceName} - {
        SourceName.SOLAR_FORECAST.value,
        SourceName.LARGE_SOLAR.value,
        SourceName.ETREL.value,
    }
    assert expected <= set(sources.keys())


async def test_tick_skips_decision_when_sonnen_unreadable(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = _config(tmp_path)
    cache = PriceCache()
    loop = TickLoop(config, session_factory, OverrideController(), cache, SolarCache())
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _install_fakes(
        loop,
        sonnen=FakeClient(SourceName.SONNEN, raise_=DeviceConnectionError("unreachable")),
        car=FakeClient(SourceName.CAR_CHARGER, data={"active_power_w": 0.0}),
        p1=FakeClient(SourceName.P1_METER, data={"active_power_w": 0.0}),
        small=FakeClient(SourceName.SMALL_SOLAR, data={"active_power_w": 0.0}),
        solaredge=FakeSolarEdge(),
        provider=FakeProvider([_hour_price(now, injection=0.05)]),
    )

    await loop.tick(now=now)

    async with UnitOfWork(session_factory) as uow:
        decision = await uow.decisions.latest()
        reading = await uow.readings.latest()
        sonnen_status = await uow.source_status.get(SourceName.SONNEN.value)

    assert decision is None
    assert reading is not None  # partial reading still recorded
    assert reading.battery_soc_pct is None
    assert sonnen_status is not None
    assert sonnen_status.last_error_message is not None
    assert "unreachable" in sonnen_status.last_error_message


async def test_tick_actuates_solaredge_on_state_change_when_not_dry_run(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = _config(tmp_path, dry_run=False)
    cache = PriceCache()
    loop = TickLoop(config, session_factory, OverrideController(), cache, SolarCache())
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    fake_se = FakeSolarEdge(data={"active_power_limit_pct": 0.0})
    _install_fakes(
        loop,
        sonnen=FakeClient(SourceName.SONNEN, data={"soc_pct": 75.0}),
        car=FakeClient(SourceName.CAR_CHARGER, data={"active_power_w": 0.0}),
        p1=FakeClient(SourceName.P1_METER, data={"active_power_w": 0.0}),
        small=FakeClient(SourceName.SMALL_SOLAR, data={"active_power_w": 0.0}),
        solaredge=fake_se,
        provider=FakeProvider([_hour_price(now, injection=0.05)]),
    )

    await loop.tick(now=now)
    # First tick: engine decides ON and the inverter still reads 0 %, so
    # _needs_actuation issues the write to 100 %.
    assert fake_se.write_calls == [100]


async def test_tick_does_not_actuate_in_dry_run(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = _config(tmp_path, dry_run=True)
    cache = PriceCache()
    loop = TickLoop(config, session_factory, OverrideController(), cache, SolarCache())
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    fake_se = FakeSolarEdge()
    _install_fakes(
        loop,
        sonnen=FakeClient(SourceName.SONNEN, data={"soc_pct": 75.0}),
        car=FakeClient(SourceName.CAR_CHARGER, data={"active_power_w": 0.0}),
        p1=FakeClient(SourceName.P1_METER, data={"active_power_w": 0.0}),
        small=FakeClient(SourceName.SMALL_SOLAR, data={"active_power_w": 0.0}),
        solaredge=fake_se,
        provider=FakeProvider([_hour_price(now, injection=0.05)]),
    )

    await loop.tick(now=now)
    assert fake_se.write_calls == []


async def test_tick_does_not_actuate_when_state_unchanged(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = _config(tmp_path, dry_run=False)
    cache = PriceCache()
    loop = TickLoop(config, session_factory, OverrideController(), cache, SolarCache())
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    fake_se = FakeSolarEdge()
    _install_fakes(
        loop,
        sonnen=FakeClient(SourceName.SONNEN, data={"soc_pct": 75.0}),
        car=FakeClient(SourceName.CAR_CHARGER, data={"active_power_w": 0.0}),
        p1=FakeClient(SourceName.P1_METER, data={"active_power_w": 0.0}),
        small=FakeClient(SourceName.SMALL_SOLAR, data={"active_power_w": 0.0}),
        solaredge=fake_se,
        provider=FakeProvider([_hour_price(now, injection=0.05)]),
    )

    # Pre-seed an ON decision so state_changed=False on the next tick.
    async with UnitOfWork(session_factory) as uow:
        await uow.decisions.add(
            Decision(
                timestamp=now - timedelta(minutes=1),
                state=DecisionState.ON.value,
                rule_fired="positive_injection_price",
                reason="seed",
            )
        )
        await uow.commit()

    await loop.tick(now=now)
    assert fake_se.write_calls == []  # no flip needed


async def test_tick_refreshes_price_cache_when_stale(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = _config(tmp_path)
    cache = PriceCache()
    loop = TickLoop(config, session_factory, OverrideController(), cache, SolarCache())
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    provider = FakeProvider([_hour_price(now, injection=0.07)])
    _install_fakes(
        loop,
        sonnen=FakeClient(SourceName.SONNEN, data={"soc_pct": 75.0}),
        car=FakeClient(SourceName.CAR_CHARGER, data={"active_power_w": 0.0}),
        p1=FakeClient(SourceName.P1_METER, data={"active_power_w": 0.0}),
        small=FakeClient(SourceName.SMALL_SOLAR, data={"active_power_w": 0.0}),
        solaredge=FakeSolarEdge(),
        provider=provider,
    )

    await loop.tick(now=now)
    assert provider.fetch_calls == 1
    assert len(cache.points()) == 1

    # Same tick again at the same instant — cache is fresh, no second fetch.
    await loop.tick(now=now)
    assert provider.fetch_calls == 1


async def test_tick_records_price_fetch_error(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = _config(tmp_path)
    cache = PriceCache()
    loop = TickLoop(config, session_factory, OverrideController(), cache, SolarCache())
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _install_fakes(
        loop,
        sonnen=FakeClient(SourceName.SONNEN, data={"soc_pct": 75.0}),
        car=FakeClient(SourceName.CAR_CHARGER, data={"active_power_w": 0.0}),
        p1=FakeClient(SourceName.P1_METER, data={"active_power_w": 0.0}),
        small=FakeClient(SourceName.SMALL_SOLAR, data={"active_power_w": 0.0}),
        solaredge=FakeSolarEdge(),
        provider=FakeProvider(raise_=PriceFetchError("ENTSO-E HTTP 503")),
    )

    await loop.tick(now=now)

    async with UnitOfWork(session_factory) as uow:
        prices_status = await uow.source_status.get(SourceName.PRICES.value)
    assert prices_status is not None
    assert prices_status.last_error_message is not None
    assert "503" in prices_status.last_error_message
    # Decision still happens; with no price data, rule 4 defaults to ON
    # (safe state — we don't curtail when we can't verify the price).
    async with UnitOfWork(session_factory) as uow:
        decision = await uow.decisions.latest()
    assert decision is not None
    assert decision.state == DecisionState.ON.value


async def test_decision_interval_gates_subsequent_ticks(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ticks within decision_interval_s of the last decision skip the engine
    and the actuator, but still write a Reading row each time."""
    config = _config(tmp_path, dry_run=False)
    config = config.model_copy(update={"decision_interval_s": 60.0})
    cache = PriceCache()
    loop = TickLoop(config, session_factory, OverrideController(), cache, SolarCache())
    fake_se = FakeSolarEdge(data={"active_power_limit_pct": 0.0})
    # ``recent(hours=...)`` uses datetime.now(UTC) as the cutoff, so anchor the
    # tick timestamps to "now" instead of a fixed historical date. Snap to
    # mid-hour so the +60 s tick stays inside the same UTC hour (otherwise
    # ``get_current_hour_price`` could miss the cached point and the engine
    # would flip to OFF for an unrelated reason).
    base = datetime.now(UTC).replace(minute=30, second=0, microsecond=0)
    _install_fakes(
        loop,
        sonnen=FakeClient(SourceName.SONNEN, data={"soc_pct": 75.0}),
        car=FakeClient(SourceName.CAR_CHARGER, data={"active_power_w": 0.0}),
        p1=FakeClient(SourceName.P1_METER, data={"active_power_w": 0.0}),
        small=FakeClient(SourceName.SMALL_SOLAR, data={"active_power_w": 0.0}),
        solaredge=fake_se,
        provider=FakeProvider([_hour_price(base, injection=0.05)]),
    )

    async def counts() -> tuple[int, int]:
        async with UnitOfWork(session_factory) as uow:
            d = list(await uow.decisions.recent(hours=1))
            r = list(await uow.readings.recent(hours=1))
        return len(d), len(r)

    # First tick: no prior decision -> engine decides ON; inverter reads 0 %, so
    # _needs_actuation fires the write to 100 %.
    await loop.tick(now=base)
    assert fake_se.write_calls == [100]
    assert await counts() == (1, 1)

    # Tick at +5 s — within the 60 s gate -> no new decision, no actuator call,
    # but a fresh Reading should still be persisted.
    await loop.tick(now=base + timedelta(seconds=5))
    assert fake_se.write_calls == [100]  # unchanged
    assert await counts() == (1, 2)

    # Tick at +60 s — gate elapsed -> engine runs again. State is still ON and
    # the inverter now reads 100 % (written on tick 1), so the self-healing
    # _needs_actuation issues no new write.
    await loop.tick(now=base + timedelta(seconds=60))
    assert fake_se.write_calls == [100]
    assert await counts() == (2, 3)
