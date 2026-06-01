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
from energy_orchestrator.prices import PricePoint
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
    app = create_app(_make_config(tmp_path), start_tick_loop=False)
    transport = ASGITransport(app=app)
    # Default Origin header so POSTs pass the same-origin CSRF guard; the
    # check compares Origin to ``{scheme}://{Host header}`` and httpx fills
    # Host from base_url.
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Origin": "http://test"},
    ) as ac:
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
    # Charger control is inactive (no tick loop in tests) -> null, key present.
    assert body["charger"] is None


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
    assert names == {
        "sonnen",
        "car_charger",
        "p1_meter",
        "small_solar",
        "large_solar",
        "solaredge",
        "etrel",
        "tronity",
        "prices",
        "solar_forecast",
    }
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
            SourceName.LARGE_SOLAR,
            SourceName.SOLAREDGE,
            SourceName.ETREL,
            SourceName.TRONITY,
            SourceName.PRICES,
            SourceName.SOLAR_FORECAST,
        ):
            await uow.source_status.record_success(name, payload={"ok": True})
        await uow.commit()

    resp = await client.get("/api/health")
    body = resp.json()
    assert body["status"] == "ok"
    assert all(s["status"] == "OK" for s in body["sources"])


async def test_config_autofills_geofence_from_device_location(client: AsyncClient) -> None:
    """GET /config pre-fills the Tronity geofence from the device's IP location.

    The location is preset on app.state so no real geolocation call is made;
    the widened radius reflects the city-level accuracy.
    """
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.device_geolocation = (51.0543, 3.7174)

    resp = await client.get("/config")
    assert resp.status_code == 200
    html = resp.text
    assert 'name="tronity.home_latitude" value="51.054300"' in html
    assert 'name="tronity.home_longitude" value="3.717400"' in html
    assert 'name="tronity.geofence_radius_m" value="25000"' in html


async def test_config_keeps_saved_geofence_over_device_location(
    client: AsyncClient, tmp_path: Path
) -> None:
    """A configured geofence is never overridden by the device-location suggestion."""
    from energy_orchestrator.config.models import TronityConfig

    app = client._transport.app  # type: ignore[attr-defined]
    app.state.device_geolocation = (10.0, 20.0)
    base = app.state.config
    app.state.config = base.model_copy(
        update={
            "tronity": TronityConfig(
                client_id="cid",
                client_secret="sec",
                home_latitude=51.5,
                home_longitude=3.5,
            )
        }
    )

    resp = await client.get("/config")
    html = resp.text
    assert 'name="tronity.home_latitude" value="51.5"' in html
    # The device suggestion (10.0) must NOT appear.
    assert 'value="10.000000"' not in html


async def test_clear_errors_nulls_error_columns(client: AsyncClient) -> None:
    """POST /api/source-status/clear-errors blanks last_error_at and
    last_error_message on every row, leaving last_success_at intact."""
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    async with UnitOfWork(factory) as uow:
        await uow.source_status.record_success(
            SourceName.SOLAREDGE, payload={"active_power_limit_pct": 100.0}
        )
        await uow.source_status.record_error(SourceName.SOLAREDGE, message="boom")
        await uow.source_status.record_error(SourceName.SONNEN, message="kaboom")
        await uow.commit()

    resp = await client.post("/api/source-status/clear-errors")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cleared"] >= 2

    async with UnitOfWork(factory) as uow:
        rows = {r.source_name: r for r in await uow.source_status.all()}
    assert rows["solaredge"].last_error_at is None
    assert rows["solaredge"].last_error_message is None
    # last_success_at is untouched.
    assert rows["solaredge"].last_success_at is not None
    assert rows["sonnen"].last_error_at is None
    assert rows["sonnen"].last_error_message is None


async def test_health_ok_when_success_follows_recent_error(client: AsyncClient) -> None:
    """A successful read after a recent error must clear the ERROR badge —
    regression test for the classifier holding ERROR for a fixed cooldown
    even though SolarEdge had recovered on the next tick."""
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    async with UnitOfWork(factory) as uow:
        # Pre-existing error from a previous (failed) tick.
        await uow.source_status.record_error(
            SourceName.SOLAREDGE, message="modbus glitch from prior session"
        )
        # Now the device recovers — successful read on this tick.
        await uow.source_status.record_success(
            SourceName.SOLAREDGE, payload={"active_power_limit_pct": 100.0}
        )
        await uow.commit()

    resp = await client.get("/api/health")
    body = resp.json()
    solaredge = next(s for s in body["sources"] if s["source_name"] == "solaredge")
    assert solaredge["status"] == "OK", (
        f"recovered source should classify OK, got {solaredge['status']} "
        f"with last_error_message={solaredge.get('last_error_message')!r}"
    )


# ----- API: /api/prices --------------------------------------------------------


async def test_prices_returns_empty_until_cache_populated(client: AsyncClient) -> None:
    resp = await client.get("/api/prices")
    body = resp.json()
    assert body["prices"] == []
    assert body["last_refresh"] is None
    assert "window_start" in body and "window_end" in body


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


# ----- API: /api/charger/mode --------------------------------------------------


class _FakeTickLoop:
    """Minimal stand-in for the tick loop's charger-mode surface."""

    def __init__(self, *, active: bool = True) -> None:
        self.active = active
        self.calls: list[tuple[str, float | None]] = []

    def set_charger_mode(self, mode: object, amps: float | None = None) -> dict[str, object]:
        self.calls.append((str(mode), amps))
        forced = str(mode) == "forced"
        return {
            "mode": str(mode),
            "forced_amps": (amps if forced else None),
            "active": self.active,
        }

    async def toggle_solaredge_limit_manual(self) -> dict[str, object]:
        return {
            "limit_before_pct": 100,
            "read_before_error": None,
            "target_pct": 0,
            "target_state": "off",
            "write_succeeded": True,
            "write_error": None,
            "active_power_limit_pct_after": 0,
            "readback_error": None,
            "took": True,
        }


async def test_charger_mode_forced_requires_amps(client: AsyncClient) -> None:
    resp = await client.post("/api/charger/mode", json={"mode": "forced"})
    assert resp.status_code == 422


async def test_charger_mode_rejects_over_16a(client: AsyncClient) -> None:
    resp = await client.post("/api/charger/mode", json={"mode": "forced", "amps": 20})
    assert resp.status_code == 422


async def test_charger_mode_rejects_invalid_mode(client: AsyncClient) -> None:
    resp = await client.post("/api/charger/mode", json={"mode": "bogus"})
    assert resp.status_code == 422


async def test_charger_mode_503_without_tick_loop(client: AsyncClient) -> None:
    # The fixture builds the app with start_tick_loop=False -> no tick loop.
    resp = await client.post("/api/charger/mode", json={"mode": "optimized"})
    assert resp.status_code == 503


async def test_charger_mode_409_when_charger_disabled(client: AsyncClient) -> None:
    client._transport.app.state.tick_loop = _FakeTickLoop(active=False)  # type: ignore[attr-defined]
    resp = await client.post("/api/charger/mode", json={"mode": "optimized"})
    assert resp.status_code == 409


async def test_charger_mode_forced_then_optimized_ok(client: AsyncClient) -> None:
    fake = _FakeTickLoop(active=True)
    client._transport.app.state.tick_loop = fake  # type: ignore[attr-defined]
    forced = await client.post("/api/charger/mode", json={"mode": "forced", "amps": 8})
    assert forced.status_code == 200
    assert forced.json()["mode"] == "forced"
    assert forced.json()["forced_amps"] == 8.0
    optimized = await client.post("/api/charger/mode", json={"mode": "optimized"})
    assert optimized.status_code == 200
    assert optimized.json()["mode"] == "optimized"
    assert optimized.json()["forced_amps"] is None
    assert ("forced", 8.0) in fake.calls


# ----- API: /api/solaredge/test-toggle -----------------------------------------


async def test_solaredge_toggle_503_without_tick_loop(client: AsyncClient) -> None:
    # The fixture builds the app with start_tick_loop=False -> no tick loop.
    resp = await client.post("/api/solaredge/test-toggle")
    assert resp.status_code == 503


async def test_solaredge_toggle_ok_with_tick_loop(client: AsyncClient) -> None:
    client._transport.app.state.tick_loop = _FakeTickLoop(active=True)  # type: ignore[attr-defined]
    resp = await client.post("/api/solaredge/test-toggle")
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_state"] == "off"
    assert body["target_pct"] == 0
    assert body["write_succeeded"] is True
    assert body["took"] is True


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


async def test_vendored_chartjs_served_offline(client: AsyncClient) -> None:
    """The chart library MUST ship with the orchestrator (no CDN, home LAN)."""
    resp = await client.get("/static/vendor/chart.umd.min.js")
    assert resp.status_code == 200
    assert "Chart.js" in resp.text


async def test_dashboard_includes_combined_chart_canvas(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert 'id="mainChart"' in resp.text
    assert "/static/vendor/chart.umd.min.js" in resp.text
    assert "/static/dashboard.js" in resp.text


async def test_prices_endpoint_returns_cached_points_after_replace(client: AsyncClient) -> None:
    cache = client._transport.app.state.price_cache  # type: ignore[attr-defined]
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

    cache.replace(
        [
            PricePoint(
                timestamp=now,
                consumption_eur_per_kwh=0.20,
                injection_eur_per_kwh=0.05,
            ),
            PricePoint(
                timestamp=now + timedelta(hours=1),
                consumption_eur_per_kwh=0.21,
                injection_eur_per_kwh=-0.01,
            ),
        ],
        now,
    )

    resp = await client.get("/api/prices")
    body = resp.json()
    assert len(body["prices"]) == 2
    assert body["last_refresh"] is not None
    assert body["prices"][1]["injection_eur_per_kwh"] == -0.01


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
        "/api/charger/mode",
    ):
        assert path in paths
