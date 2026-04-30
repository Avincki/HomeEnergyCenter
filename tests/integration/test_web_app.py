from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

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
    Reading,
    SourceName,
    UnitOfWork,
)
from energy_orchestrator.web.app import create_app


def _make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        poll_interval_s=30.0,
        sonnen=SonnenBatterieConfig(
            host="192.168.1.50",
            api_version=SonnenApiVersion.V2,
            auth_token="dummy",
            capacity_kwh=10.0,
        ),
        homewizard=HomeWizardConfig(
            car_charger=CarChargerConfig(host="192.168.1.51"),
            p1_meter=P1MeterConfig(host="192.168.1.52"),
            small_solar=SmallSolarConfig(host="192.168.1.53", peak_w=2000.0),
        ),
        solaredge=SolarEdgeConfig(host="192.168.1.60"),
        prices=PricesConfig(provider=PricesProvider.ENTSOE, api_key="dummy"),
        decision=DecisionConfig(),
        storage=StorageConfig(sqlite_path=tmp_path / "test.db"),
        logging=LoggingConfig(log_dir=tmp_path / "logs"),
        web=WebConfig(),
    )


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    app = create_app(_make_config(tmp_path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Trigger the lifespan startup so app.state is populated.
        async with app.router.lifespan_context(app):
            yield ac


# ----- API: /api/state ---------------------------------------------------------


async def test_state_empty_when_no_data(client: AsyncClient) -> None:
    resp = await client.get("/api/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reading"] is None
    assert body["decision"] is None
    assert body["sources"] == []
    assert body["override"]["mode"] == "auto"


async def test_state_returns_latest_after_inserts(client: AsyncClient) -> None:
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    async with UnitOfWork(factory) as uow:
        await uow.readings.add(Reading(battery_soc_pct=75.0, battery_power_w=-200.0))
        await uow.decisions.add(
            Decision(
                state=DecisionState.ON,
                rule_fired="positive_injection_price",
                reason="injection > 0",
            )
        )
        await uow.source_status.record_success(SourceName.SONNEN, payload={"USOC": 75})
        await uow.commit()

    resp = await client.get("/api/state")
    body = resp.json()
    assert body["reading"]["battery_soc_pct"] == 75.0
    assert body["decision"]["state"] == "on"
    assert any(s["source_name"] == "sonnen" for s in body["sources"])


# ----- API: /api/history -------------------------------------------------------


async def test_history_filters_by_window(client: AsyncClient) -> None:
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    now = datetime.now(UTC)
    async with UnitOfWork(factory) as uow:
        await uow.readings.add(Reading(timestamp=now - timedelta(hours=2), battery_soc_pct=60.0))
        await uow.readings.add(Reading(timestamp=now - timedelta(hours=10), battery_soc_pct=50.0))
        await uow.commit()

    resp = await client.get("/api/history?h=4")
    body = resp.json()
    assert body["hours"] == 4
    assert len(body["readings"]) == 1


async def test_history_rejects_invalid_window(client: AsyncClient) -> None:
    resp = await client.get("/api/history?h=0")
    assert resp.status_code == 422


# ----- API: /api/sources -------------------------------------------------------


async def test_sources_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/sources")
    assert resp.status_code == 200
    assert resp.json() == []


# ----- API: /api/health --------------------------------------------------------


async def test_health_lists_all_expected_sources(client: AsyncClient) -> None:
    resp = await client.get("/api/health")
    body = resp.json()
    names = {s["source_name"] for s in body["sources"]}
    assert names == {"sonnen", "car_charger", "p1_meter", "small_solar", "solaredge", "prices"}
    assert body["status"] == "degraded"  # all UNKNOWN
    assert body["dry_run"] is True


async def test_health_ok_when_recent_success(client: AsyncClient) -> None:
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    async with UnitOfWork(factory) as uow:
        # Record successes for every expected source.
        for name in (
            SourceName.SONNEN,
            SourceName.CAR_CHARGER,
            SourceName.P1_METER,
            SourceName.SMALL_SOLAR,
            SourceName.SOLAREDGE,
            SourceName.PRICES,
        ):
            await uow.source_status.record_success(name, payload={"ok": True})
        await uow.commit()

    resp = await client.get("/api/health")
    body = resp.json()
    assert body["status"] == "ok"
    assert all(s["status"] == "OK" for s in body["sources"])


# ----- API: /api/prices --------------------------------------------------------


async def test_prices_returns_empty_with_note(client: AsyncClient) -> None:
    resp = await client.get("/api/prices")
    body = resp.json()
    assert body["prices"] == []
    assert "tick loop" in body["note"]


# ----- API: /api/override ------------------------------------------------------


async def test_override_force_on_indefinite(client: AsyncClient) -> None:
    resp = await client.post("/api/override", json={"mode": "force_on"})
    assert resp.status_code == 200
    assert resp.json() == {"mode": "force_on", "expires_at": None}

    state_resp = await client.get("/api/state")
    assert state_resp.json()["override"]["mode"] == "force_on"


async def test_override_with_minutes_sets_expiry(client: AsyncClient) -> None:
    resp = await client.post("/api/override", json={"mode": "force_off", "minutes": 30})
    body = resp.json()
    assert body["mode"] == "force_off"
    assert body["expires_at"] is not None


async def test_override_auto_clears(client: AsyncClient) -> None:
    await client.post("/api/override", json={"mode": "force_on"})
    resp = await client.post("/api/override", json={"mode": "auto"})
    assert resp.json()["mode"] == "auto"


async def test_override_auto_with_minutes_rejected(client: AsyncClient) -> None:
    resp = await client.post("/api/override", json={"mode": "auto", "minutes": 10})
    assert resp.status_code == 400


async def test_override_invalid_mode_rejected(client: AsyncClient) -> None:
    resp = await client.post("/api/override", json={"mode": "totally_not_a_mode"})
    assert resp.status_code == 422


async def test_override_minutes_out_of_range_rejected(client: AsyncClient) -> None:
    resp = await client.post("/api/override", json={"mode": "force_on", "minutes": 0})
    assert resp.status_code == 422


# ----- HTML views --------------------------------------------------------------


async def test_dashboard_renders_html(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Energy Orchestrator" in resp.text
    assert "No decision recorded yet" in resp.text


async def test_dashboard_with_data(client: AsyncClient) -> None:
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    async with UnitOfWork(factory) as uow:
        await uow.readings.add(Reading(battery_soc_pct=72.0))
        await uow.decisions.add(
            Decision(
                state=DecisionState.ON,
                rule_fired="positive_injection_price",
                reason="injection price 0.05 EUR/kWh > 0",
            )
        )
        await uow.commit()

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "72.0%" in resp.text
    assert "positive_injection_price" in resp.text


async def test_debug_board_renders(client: AsyncClient) -> None:
    resp = await client.get("/debug")
    assert resp.status_code == 200
    assert "Source health" in resp.text
    assert "Manual override" in resp.text
    assert "Active config" in resp.text
    # Sensitive values should be redacted.
    assert "dummy" not in resp.text  # sonnen.auth_token raw value never in HTML


async def test_static_css_served(client: AsyncClient) -> None:
    resp = await client.get("/static/style.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]


async def test_openapi_includes_all_endpoints(client: AsyncClient) -> None:
    resp = await client.get("/openapi.json")
    paths = resp.json()["paths"]
    for path in (
        "/api/state",
        "/api/history",
        "/api/sources",
        "/api/health",
        "/api/prices",
        "/api/override",
    ):
        assert path in paths
