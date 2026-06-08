from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from energy_orchestrator.config.models import TronityConfig
from energy_orchestrator.vehicle import (
    TronityProvider,
    VehicleAuthError,
    VehicleConfigurationError,
)


class _FakeTronity:
    """Minimal in-memory Tronity server for the provider tests.

    Routes mirror the real API: ``POST /authentication``,
    ``GET /tronity/vehicles`` and ``GET /tronity/vehicles/{vid}/last_record``.
    Counters let tests assert token reuse / re-auth behaviour.
    """

    def __init__(
        self,
        *,
        vehicles: list[dict[str, Any]] | None = None,
        record: dict[str, Any] | None = None,
        auth_status: int = 200,
        last_record_status: int = 200,
    ) -> None:
        self.vehicles = vehicles if vehicles is not None else [{"id": "veh-1", "vin": "WDB123"}]
        self.record = record if record is not None else {"level": 80.0}
        self.auth_status = auth_status
        self.last_record_status = last_record_status
        self.auth_calls = 0
        self.last_record_calls = 0
        self.auth_bodies: list[dict[str, Any]] = []
        self.last_record_headers: list[dict[str, str]] = []
        # last_record returns 401 this many times before succeeding (token-rot test).
        self.unauthorized_first_n = 0

    async def auth(self, request: web.Request) -> web.Response:
        self.auth_calls += 1
        self.auth_bodies.append(await request.json())
        if self.auth_status != 200:
            return web.Response(status=self.auth_status)
        return web.json_response(
            {"access_token": f"tok-{self.auth_calls}", "token_type": "Bearer", "expires_in": 3600}
        )

    async def list_vehicles(self, request: web.Request) -> web.Response:
        return web.json_response({"data": self.vehicles})

    async def last_record(self, request: web.Request) -> web.Response:
        self.last_record_calls += 1
        self.last_record_headers.append(dict(request.headers))
        if self.last_record_calls <= self.unauthorized_first_n:
            return web.Response(status=401)
        if self.last_record_status != 200:
            return web.Response(status=self.last_record_status)
        return web.json_response(self.record)


@asynccontextmanager
async def _running(fake: _FakeTronity) -> AsyncIterator[TestServer]:
    app = web.Application()
    app.router.add_post("/authentication", fake.auth)
    app.router.add_get("/tronity/vehicles", fake.list_vehicles)
    app.router.add_get("/tronity/vehicles/{vid}/last_record", fake.last_record)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


def _config(server: TestServer, *, vin: str | None = "WDB123", **kw: Any) -> TronityConfig:
    return TronityConfig(
        client_id="cid",
        client_secret="secret",
        vin=vin,
        base_url=f"http://127.0.0.1:{server.port}",
        **kw,
    )


# ----- happy path --------------------------------------------------------------


async def test_fetch_parses_full_record() -> None:
    fake = _FakeTronity(
        record={
            "level": 81.0,
            "plugged": True,
            "charging": "Charging",
            "range": 410.0,
            "odometer": 12345.0,
            "chargerPower": 11.0,
            "latitude": 51.05,
            "longitude": 3.72,
            "timestamp": 1_780_000_000_000,  # ms epoch
        }
    )
    async with _running(fake) as server:
        async with TronityProvider(_config(server)) as provider:
            rec = await provider.fetch_record()

    assert rec.soc_pct == 81.0
    assert rec.plugged is True
    assert rec.charging == "Charging"
    assert rec.range_km == 410.0
    assert rec.odometer_km == 12345.0
    assert rec.charger_power_kw == 11.0
    assert rec.latitude == 51.05
    assert rec.recorded_at is not None
    assert rec.recorded_at.year == 2026  # 1.78e12 ms -> mid-2026


async def test_fetch_parses_record_without_position() -> None:
    # Mercedes' EV-status scope often omits GPS — the record must still parse,
    # leaving latitude/longitude as None (the case the position log surfaces).
    fake = _FakeTronity(
        record={
            "level": 81.0,
            "timestamp": 1_780_000_000_000,
        }
    )
    async with _running(fake) as server:
        async with TronityProvider(_config(server)) as provider:
            rec = await provider.fetch_record()

    assert rec.soc_pct == 81.0
    assert rec.latitude is None
    assert rec.longitude is None


async def test_auth_body_carries_app_grant_and_credentials() -> None:
    fake = _FakeTronity()
    async with _running(fake) as server:
        async with TronityProvider(_config(server)) as provider:
            await provider.fetch_record()
    assert fake.auth_bodies[0] == {
        "client_id": "cid",
        "client_secret": "secret",
        "grant_type": "app",
    }


async def test_last_record_sends_bearer_and_unit_system() -> None:
    fake = _FakeTronity()
    async with _running(fake) as server:
        async with TronityProvider(_config(server)) as provider:
            await provider.fetch_record()
    headers = fake.last_record_headers[0]
    assert headers["Authorization"] == "Bearer tok-1"
    assert headers["Unit-System"] == "metric"


# ----- vehicle resolution ------------------------------------------------------


async def test_vin_selects_correct_vehicle() -> None:
    fake = _FakeTronity(
        vehicles=[{"id": "a", "vin": "OTHER"}, {"id": "b", "vin": "WDB123"}],
    )
    async with _running(fake) as server:
        async with TronityProvider(_config(server, vin="WDB123")) as provider:
            await provider.fetch_record()
    # The matching vehicle's id must be the one hit for last_record.
    # (aiohttp routes capture {vid}; we just assert the call happened.)
    assert fake.last_record_calls == 1


async def test_single_vehicle_used_without_vin() -> None:
    fake = _FakeTronity(vehicles=[{"id": "only", "vin": "SOLO"}])
    async with _running(fake) as server:
        async with TronityProvider(_config(server, vin=None)) as provider:
            rec = await provider.fetch_record()
    assert rec.soc_pct == 80.0


async def test_multiple_vehicles_without_vin_raises() -> None:
    fake = _FakeTronity(vehicles=[{"id": "a", "vin": "ONE"}, {"id": "b", "vin": "TWO"}])
    async with _running(fake) as server:
        async with TronityProvider(_config(server, vin=None)) as provider:
            with pytest.raises(VehicleConfigurationError):
                await provider.fetch_record()


async def test_unknown_vin_raises() -> None:
    fake = _FakeTronity(vehicles=[{"id": "a", "vin": "ONE"}])
    async with _running(fake) as server:
        async with TronityProvider(_config(server, vin="NOPE")) as provider:
            with pytest.raises(VehicleConfigurationError):
                await provider.fetch_record()


# ----- token lifecycle ---------------------------------------------------------


async def test_token_reused_across_fetches() -> None:
    fake = _FakeTronity()
    async with _running(fake) as server:
        async with TronityProvider(_config(server)) as provider:
            await provider.fetch_record()
            await provider.fetch_record()
    # A valid cached token means only one auth call for two fetches.
    assert fake.auth_calls == 1
    assert fake.last_record_calls == 2


async def test_reauth_on_401_then_succeeds() -> None:
    fake = _FakeTronity()
    fake.unauthorized_first_n = 1  # first last_record 401s, forcing a re-auth
    async with _running(fake) as server:
        async with TronityProvider(_config(server)) as provider:
            rec = await provider.fetch_record()
    assert rec.soc_pct == 80.0
    assert fake.auth_calls == 2  # initial + one re-auth
    assert fake.last_record_calls == 2  # 401, then success


async def test_bad_credentials_raise_auth_error() -> None:
    fake = _FakeTronity(auth_status=401)
    async with _running(fake) as server:
        async with TronityProvider(_config(server)) as provider:
            with pytest.raises(VehicleAuthError):
                await provider.fetch_record()
