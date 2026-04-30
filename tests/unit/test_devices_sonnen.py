from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from energy_orchestrator.config.models import SonnenApiVersion, SonnenBatterieConfig
from energy_orchestrator.devices import (
    DeviceConfigurationError,
    DeviceConnectionError,
    DeviceProtocolError,
    DeviceTimeoutError,
    SonnenClient,
)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@asynccontextmanager
async def _running_server(handler: Handler) -> AsyncIterator[TestServer]:
    app = web.Application()
    app.router.add_get("/api/v2/status", handler)
    app.router.add_get("/api/v1/status", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


def _v2_config(
    server: TestServer, *, retry_count: int = 1, timeout_s: float = 2.0
) -> SonnenBatterieConfig:
    assert server.port is not None
    return SonnenBatterieConfig(
        host="127.0.0.1",
        port=server.port,
        api_version=SonnenApiVersion.V2,
        auth_token="my-secret-token",
        capacity_kwh=10.0,
        timeout_s=timeout_s,
        retry_count=retry_count,
    )


def _v1_config(
    server: TestServer, *, retry_count: int = 1, timeout_s: float = 2.0
) -> SonnenBatterieConfig:
    assert server.port is not None
    return SonnenBatterieConfig(
        host="127.0.0.1",
        port=server.port,
        api_version=SonnenApiVersion.V1,
        capacity_kwh=10.0,
        timeout_s=timeout_s,
        retry_count=retry_count,
    )


# ----- happy path --------------------------------------------------------------


async def test_v2_success_returns_normalized_reading() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "USOC": 72,
                "Pac_total_W": -250,
                "Production_W": 1500,
                "Consumption_W": 800,
                "GridFeedIn_W": -150,
                "BatteryCharging": True,  # extra field — ignored
            }
        )

    async with _running_server(handler) as server, SonnenClient(_v2_config(server)) as client:
        reading = await client.read_data()

    assert reading is not None
    assert reading.device_id == "sonnen"
    assert reading.data == {
        "soc_pct": 72.0,
        "battery_power_w": -250.0,
        "production_w": 1500.0,
        "consumption_w": 800.0,
        "grid_feed_in_w": -150.0,
    }
    assert reading.quality == 1.0


async def test_v2_sends_auth_token_header() -> None:
    captured: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        captured["auth"] = request.headers.get("Auth-Token", "<missing>")
        return web.json_response({"USOC": 50, "Pac_total_W": 0})

    async with _running_server(handler) as server, SonnenClient(_v2_config(server)) as client:
        await client.read_data()

    assert captured["auth"] == "my-secret-token"


async def test_v1_omits_auth_token_header() -> None:
    captured: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        captured["auth"] = request.headers.get("Auth-Token", "<missing>")
        return web.json_response({"USOC": 50, "Pac_total_W": 0})

    async with _running_server(handler) as server, SonnenClient(_v1_config(server)) as client:
        await client.read_data()

    assert captured["auth"] == "<missing>"


# ----- error paths -------------------------------------------------------------


async def test_401_raises_configuration_error() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.Response(status=401, text="bad token")

    async with _running_server(handler) as server, SonnenClient(_v2_config(server)) as client:
        with pytest.raises(DeviceConfigurationError, match="401"):
            await client.read_data()


async def test_500_retries_then_raises_connection_error() -> None:
    counter = {"n": 0}

    async def handler(_: web.Request) -> web.Response:
        counter["n"] += 1
        return web.Response(status=500, text="boom")

    async with (
        _running_server(handler) as server,
        SonnenClient(_v2_config(server, retry_count=3)) as client,
    ):
        with pytest.raises(DeviceConnectionError, match="500"):
            await client.read_data()

    assert counter["n"] == 3


async def test_401_does_not_retry() -> None:
    counter = {"n": 0}

    async def handler(_: web.Request) -> web.Response:
        counter["n"] += 1
        return web.Response(status=401)

    async with (
        _running_server(handler) as server,
        SonnenClient(_v2_config(server, retry_count=3)) as client,
    ):
        with pytest.raises(DeviceConfigurationError):
            await client.read_data()

    assert counter["n"] == 1


async def test_invalid_json_raises_protocol_error() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.Response(status=200, text="<html>not json</html>", content_type="text/html")

    async with _running_server(handler) as server, SonnenClient(_v2_config(server)) as client:
        with pytest.raises(DeviceProtocolError):
            await client.read_data()


async def test_non_dict_response_raises_protocol_error() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response([1, 2, 3])

    async with _running_server(handler) as server, SonnenClient(_v2_config(server)) as client:
        with pytest.raises(DeviceProtocolError, match="not a JSON object"):
            await client.read_data()


async def test_out_of_range_soc_raises_protocol_error() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"USOC": 150, "Pac_total_W": 0})

    async with _running_server(handler) as server, SonnenClient(_v2_config(server)) as client:
        with pytest.raises(DeviceProtocolError, match="out of range"):
            await client.read_data()


async def test_connection_refused_raises_connection_error() -> None:
    config = SonnenBatterieConfig(
        host="127.0.0.1",
        port=1,  # no server here
        api_version=SonnenApiVersion.V2,
        auth_token="t",
        capacity_kwh=10.0,
        timeout_s=2.0,
        retry_count=1,
    )
    async with SonnenClient(config) as client:
        with pytest.raises(DeviceConnectionError):
            await client.read_data()


async def test_timeout_raises_timeout_error() -> None:
    async def handler(_: web.Request) -> web.Response:
        await asyncio.sleep(0.5)
        return web.json_response({"USOC": 50})

    async with (
        _running_server(handler) as server,
        SonnenClient(_v2_config(server, retry_count=1, timeout_s=0.1)) as client,
    ):
        with pytest.raises(DeviceTimeoutError):
            await client.read_data()


# ----- partial / missing -------------------------------------------------------


async def test_missing_usoc_returns_none() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"Pac_total_W": -100, "Production_W": 500})

    async with _running_server(handler) as server, SonnenClient(_v2_config(server)) as client:
        reading = await client.read_data()

    assert reading is None


async def test_partial_response_reduces_quality() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"USOC": 60, "Pac_total_W": 100, "Production_W": 500})

    async with _running_server(handler) as server, SonnenClient(_v2_config(server)) as client:
        reading = await client.read_data()

    assert reading is not None
    assert reading.quality == pytest.approx(3 / 5)
    assert "consumption_w" not in reading.data
    assert "grid_feed_in_w" not in reading.data


async def test_non_numeric_field_silently_dropped() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "USOC": 50,
                "Pac_total_W": "not-a-number",
                "Production_W": 800,
                "Consumption_W": 400,
                "GridFeedIn_W": 100,
            }
        )

    async with _running_server(handler) as server, SonnenClient(_v2_config(server)) as client:
        reading = await client.read_data()

    assert reading is not None
    assert "battery_power_w" not in reading.data
    assert reading.quality == pytest.approx(4 / 5)


# ----- health check ------------------------------------------------------------


async def test_health_check_true_on_success() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"USOC": 50})

    async with _running_server(handler) as server, SonnenClient(_v2_config(server)) as client:
        assert await client.health_check() is True


async def test_health_check_false_on_401() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.Response(status=401)

    async with _running_server(handler) as server, SonnenClient(_v2_config(server)) as client:
        assert await client.health_check() is False


async def test_health_check_false_on_unreachable() -> None:
    config = SonnenBatterieConfig(
        host="127.0.0.1",
        port=1,
        api_version=SonnenApiVersion.V2,
        auth_token="t",
        capacity_kwh=10.0,
        timeout_s=2.0,
        retry_count=1,
    )
    async with SonnenClient(config) as client:
        assert await client.health_check() is False
