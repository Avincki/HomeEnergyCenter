from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from energy_orchestrator.utils.geolocation import detect_device_location


@asynccontextmanager
async def _server(handler: Any) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_get("/json", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield f"http://127.0.0.1:{server.port}/json"
    finally:
        await server.close()


async def test_parses_latitude_longitude() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"latitude": 51.0543, "longitude": 3.7174, "city": "Gent"})

    async with _server(handler) as url:
        loc = await detect_device_location(url=url)
    assert loc == (51.0543, 3.7174)


async def test_accepts_lat_lon_spelling() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"lat": 50.85, "lon": 4.35})

    async with _server(handler) as url:
        loc = await detect_device_location(url=url)
    assert loc == (50.85, 4.35)


async def test_http_error_returns_none() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.Response(status=503)

    async with _server(handler) as url:
        assert await detect_device_location(url=url) is None


async def test_missing_fields_returns_none() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"city": "Gent"})

    async with _server(handler) as url:
        assert await detect_device_location(url=url) is None


async def test_out_of_range_returns_none() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"latitude": 999.0, "longitude": 3.7})

    async with _server(handler) as url:
        assert await detect_device_location(url=url) is None


async def test_unreachable_host_returns_none() -> None:
    # Nothing listening on this port -> connection error -> None (never raises).
    loc = await detect_device_location(url="http://127.0.0.1:1/json", timeout_s=1.0)
    assert loc is None
