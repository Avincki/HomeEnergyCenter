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
    DecisionConfig,
    HomeWizardConfig,
    LoggingConfig,
    P1MeterConfig,
    PricesConfig,
    PricesProvider,
    SmallSolarConfig,
    SolarEdgeConfig,
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
from energy_orchestrator.devices import DeviceReading
from energy_orchestrator.devices.errors import DeviceConnectionError
from energy_orchestrator.orchestrator import TickLoop
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
    expected = (
        {s.value for s in SourceName}
        - {
            SourceName.SOLAR_FORECAST.value,
            SourceName.LARGE_SOLAR.value,
            SourceName.ETREL.value,
        }
    )
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
    # First tick sees previous_state=None -> state_changed=True -> actuate.
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
    fake_se = FakeSolarEdge()
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

    # First tick: no prior decision -> engine runs, actuator fires (state flip
    # from None to ON).
    await loop.tick(now=base)
    assert fake_se.write_calls == [100]
    assert await counts() == (1, 1)

    # Tick at +5 s — within the 60 s gate -> no new decision, no actuator call,
    # but a fresh Reading should still be persisted.
    await loop.tick(now=base + timedelta(seconds=5))
    assert fake_se.write_calls == [100]  # unchanged
    assert await counts() == (1, 2)

    # Tick at +60 s — gate elapsed -> engine runs again. State is unchanged
    # (still ON), so no new actuator write.
    await loop.tick(now=base + timedelta(seconds=60))
    assert fake_se.write_calls == [100]
    assert await counts() == (2, 3)
