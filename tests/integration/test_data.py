from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from energy_orchestrator.data import (
    Decision,
    DecisionState,
    OverrideMode,
    Reading,
    SourceName,
    UnitOfWork,
    create_engine,
    create_session_factory,
    init_schema,
)


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    db_path = tmp_path / "test.db"
    eng = create_engine(db_path)
    await init_schema(eng)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


# ----- readings ----------------------------------------------------------------


async def test_add_and_fetch_reading(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with UnitOfWork(session_factory) as uow:
        await uow.readings.add(
            Reading(battery_soc_pct=72.5, battery_power_w=-300.0, production_w=1500.0)
        )
        await uow.commit()

    async with UnitOfWork(session_factory) as uow:
        latest = await uow.readings.latest()
        assert latest is not None
        assert latest.battery_soc_pct == 72.5
        assert latest.battery_power_w == -300.0


async def test_readings_recent_filter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with UnitOfWork(session_factory) as uow:
        await uow.readings.add(Reading(timestamp=now - timedelta(hours=2), battery_soc_pct=50.0))
        await uow.readings.add(Reading(timestamp=now - timedelta(minutes=10), battery_soc_pct=55.0))
        await uow.commit()

    async with UnitOfWork(session_factory) as uow:
        last_hour = await uow.readings.recent(hours=1)
        assert len(last_hour) == 1
        assert last_hour[0].battery_soc_pct == 55.0


async def test_readings_prune(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with UnitOfWork(session_factory) as uow:
        await uow.readings.add(Reading(timestamp=now - timedelta(days=10), battery_soc_pct=10.0))
        await uow.readings.add(Reading(timestamp=now - timedelta(days=1), battery_soc_pct=20.0))
        await uow.commit()

    async with UnitOfWork(session_factory) as uow:
        deleted = await uow.readings.prune(retention_days=7)
        await uow.commit()
        assert deleted == 1

    async with UnitOfWork(session_factory) as uow:
        survivors = await uow.readings.recent(hours=24 * 30)
        assert len(survivors) == 1
        assert survivors[0].battery_soc_pct == 20.0


# ----- decisions ---------------------------------------------------------------


async def test_decisions_add_and_latest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with UnitOfWork(session_factory) as uow:
        await uow.decisions.add(
            Decision(
                state=DecisionState.OFF,
                rule_fired="rule_4_forecast_saturated",
                reason="Battery would saturate from small string alone",
                state_changed=True,
                manual_override=False,
                forecast_end_soc_pct=85.0,
            )
        )
        await uow.decisions.add(
            Decision(
                state=DecisionState.ON,
                rule_fired="manual_override",
                reason="user forced ON for 30 min",
                state_changed=True,
                manual_override=True,
                override_mode=OverrideMode.FORCE_ON,
            )
        )
        await uow.commit()

    async with UnitOfWork(session_factory) as uow:
        latest = await uow.decisions.latest()
        assert latest is not None
        assert latest.state == DecisionState.ON
        assert latest.manual_override is True
        assert latest.override_mode == OverrideMode.FORCE_ON


# ----- source_status -----------------------------------------------------------


async def test_source_status_upsert_success_then_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with UnitOfWork(session_factory) as uow:
        await uow.source_status.record_success(
            SourceName.SONNEN, payload={"USOC": 72, "Pac_total_W": -250}
        )
        await uow.commit()

    async with UnitOfWork(session_factory) as uow:
        status = await uow.source_status.get(SourceName.SONNEN)
        assert status is not None
        assert status.last_success_at is not None
        assert status.last_error_at is None
        assert status.last_payload == {"USOC": 72, "Pac_total_W": -250}

        await uow.source_status.record_error(SourceName.SONNEN, "connection refused")
        await uow.commit()

    async with UnitOfWork(session_factory) as uow:
        status = await uow.source_status.get(SourceName.SONNEN)
        assert status is not None
        # Success row preserved when an error follows.
        assert status.last_success_at is not None
        assert status.last_error_at is not None
        assert status.last_error_message == "connection refused"
        # Payload from previous success should still be there.
        assert status.last_payload == {"USOC": 72, "Pac_total_W": -250}


async def test_source_status_all_returns_every_source(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with UnitOfWork(session_factory) as uow:
        for name in (SourceName.SONNEN, SourceName.SOLAREDGE, SourceName.PRICES):
            await uow.source_status.record_success(name, payload={"ok": True})
        await uow.commit()

    async with UnitOfWork(session_factory) as uow:
        rows = await uow.source_status.all()
        assert {r.source_name for r in rows} == {"sonnen", "solaredge", "prices"}


# ----- unit of work ------------------------------------------------------------


async def test_uow_rolls_back_on_exception(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with UnitOfWork(session_factory) as uow:
            await uow.readings.add(Reading(battery_soc_pct=99.0))
            raise RuntimeError("boom")

    async with UnitOfWork(session_factory) as uow:
        latest = await uow.readings.latest()
        assert latest is None


async def test_uow_no_commit_means_no_persist(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with UnitOfWork(session_factory) as uow:
        await uow.readings.add(Reading(battery_soc_pct=42.0))
        # no commit

    async with UnitOfWork(session_factory) as uow:
        latest = await uow.readings.latest()
        assert latest is None


async def test_uow_session_property_raises_outside_context(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    uow = UnitOfWork(session_factory)
    with pytest.raises(RuntimeError, match="not active"):
        _ = uow.session
