from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from energy_orchestrator.config.models import PricesConfig, PricesProvider
from energy_orchestrator.prices import (
    EntsoePriceProvider,
    PriceConfigurationError,
    PriceFetchError,
    PriceParseError,
)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@asynccontextmanager
async def _running_server(handler: Handler) -> AsyncIterator[TestServer]:
    app = web.Application()
    app.router.add_get("/api", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


def _config(*, area: str = "BE", factor: float = 1.0, offset: float = 0.0) -> PricesConfig:
    return PricesConfig(
        provider=PricesProvider.ENTSOE,
        api_key="dummy-key",
        area=area,
        injection_factor=factor,
        injection_offset=offset,
    )


def _base_url(server: TestServer) -> str:
    return f"http://127.0.0.1:{server.port}/api"


def _make_xml(periods: list[tuple[str, str, str, list[tuple[int, float]]]]) -> str:
    """Build a minimal ENTSO-E response. ``periods`` is a list of
    (start, end, resolution, [(position, price), ...]) tuples."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Publication_MarketDocument>"]
    for start, end, resolution, points in periods:
        parts.append("<TimeSeries><Period>")
        parts.append(f"<timeInterval><start>{start}</start><end>{end}</end></timeInterval>")
        parts.append(f"<resolution>{resolution}</resolution>")
        for pos, price in points:
            parts.append(
                f"<Point><position>{pos}</position><price.amount>{price}</price.amount></Point>"
            )
        parts.append("</Period></TimeSeries>")
    parts.append("</Publication_MarketDocument>")
    return "".join(parts)


# ----- happy path --------------------------------------------------------------


async def test_successful_fetch_returns_parsed_points() -> None:
    captured: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        captured.update(request.query)
        xml = _make_xml(
            [
                (
                    "2026-04-30T00:00Z",
                    "2026-04-30T03:00Z",
                    "PT60M",
                    [(1, 50.0), (2, 45.0), (3, -10.0)],
                )
            ]
        )
        return web.Response(text=xml, content_type="application/xml")

    async with _running_server(handler) as server:
        async with EntsoePriceProvider(_config(), base_url=_base_url(server)) as provider:
            points = list(
                await provider.fetch_prices(
                    datetime(2026, 4, 30, tzinfo=UTC),
                    datetime(2026, 4, 30, 3, tzinfo=UTC),
                )
            )

    assert len(points) == 3
    # EUR/MWh -> EUR/kWh
    assert points[0].consumption_eur_per_kwh == pytest.approx(0.050)
    assert points[1].consumption_eur_per_kwh == pytest.approx(0.045)
    assert points[2].consumption_eur_per_kwh == pytest.approx(-0.010)
    # injection_factor=1.0, offset=0.0 -> injection == consumption
    assert points[0].injection_eur_per_kwh == pytest.approx(0.050)
    assert points[2].injection_eur_per_kwh == pytest.approx(-0.010)
    # Hour-aligned timestamps starting from period start
    assert points[0].timestamp == datetime(2026, 4, 30, 0, tzinfo=UTC)
    assert points[1].timestamp == datetime(2026, 4, 30, 1, tzinfo=UTC)
    assert points[2].timestamp == datetime(2026, 4, 30, 2, tzinfo=UTC)


async def test_request_carries_token_and_eic_for_belgium() -> None:
    captured: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        captured.update(request.query)
        return web.Response(
            text=_make_xml([("2026-04-30T00:00Z", "2026-04-30T01:00Z", "PT60M", [(1, 50.0)])]),
            content_type="application/xml",
        )

    async with _running_server(handler) as server:
        async with EntsoePriceProvider(_config(), base_url=_base_url(server)) as provider:
            await provider.fetch_prices(
                datetime(2026, 4, 30, tzinfo=UTC),
                datetime(2026, 5, 1, tzinfo=UTC),
            )

    assert captured["securityToken"] == "dummy-key"
    assert captured["documentType"] == "A44"
    assert captured["in_Domain"] == "10YBE----------2"
    assert captured["out_Domain"] == "10YBE----------2"
    assert captured["periodStart"] == "202604300000"
    assert captured["periodEnd"] == "202605010000"


async def test_unknown_area_passes_through_as_eic() -> None:
    captured: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        captured.update(request.query)
        return web.Response(text=_make_xml([]), content_type="application/xml")

    raw_eic = "10Y1001A1001A82H"  # Germany-Luxembourg
    async with (
        _running_server(handler) as server,
        EntsoePriceProvider(_config(area=raw_eic), base_url=_base_url(server)) as provider,
    ):
        await provider.fetch_prices(
            datetime(2026, 4, 30, tzinfo=UTC),
            datetime(2026, 5, 1, tzinfo=UTC),
        )

    assert captured["in_Domain"] == raw_eic


async def test_injection_factor_and_offset_applied() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.Response(
            text=_make_xml([("2026-04-30T00:00Z", "2026-04-30T01:00Z", "PT60M", [(1, 100.0)])]),
            content_type="application/xml",
        )

    async with (
        _running_server(handler) as server,
        EntsoePriceProvider(
            _config(factor=0.95, offset=-0.02), base_url=_base_url(server)
        ) as provider,
    ):
        points = list(
            await provider.fetch_prices(
                datetime(2026, 4, 30, tzinfo=UTC),
                datetime(2026, 5, 1, tzinfo=UTC),
            )
        )

    # wholesale = 100 EUR/MWh = 0.10 EUR/kWh
    # injection = 0.10 * 0.95 + (-0.02) = 0.075
    assert points[0].consumption_eur_per_kwh == pytest.approx(0.10)
    assert points[0].injection_eur_per_kwh == pytest.approx(0.075)


# ----- fill-forward & period structure -----------------------------------------


async def test_fill_forward_for_sparse_positions() -> None:
    """Per ENTSO-E convention, missing positions inherit the previous price."""

    async def handler(_: web.Request) -> web.Response:
        return web.Response(
            text=_make_xml(
                [
                    (
                        "2026-04-30T00:00Z",
                        "2026-04-30T05:00Z",
                        "PT60M",
                        # 5 hours; only positions 1, 3 and 5 reported.
                        [(1, 50.0), (3, 60.0), (5, 70.0)],
                    )
                ]
            ),
            content_type="application/xml",
        )

    async with _running_server(handler) as server:
        async with EntsoePriceProvider(_config(), base_url=_base_url(server)) as provider:
            points = list(
                await provider.fetch_prices(
                    datetime(2026, 4, 30, tzinfo=UTC),
                    datetime(2026, 4, 30, 5, tzinfo=UTC),
                )
            )

    assert len(points) == 5
    expected = [0.050, 0.050, 0.060, 0.060, 0.070]
    assert [p.consumption_eur_per_kwh for p in points] == pytest.approx(expected)


async def test_multiple_periods_concatenated() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.Response(
            text=_make_xml(
                [
                    (
                        "2026-04-30T00:00Z",
                        "2026-04-30T02:00Z",
                        "PT60M",
                        [(1, 50.0), (2, 45.0)],
                    ),
                    (
                        "2026-04-30T02:00Z",
                        "2026-04-30T04:00Z",
                        "PT60M",
                        [(1, 30.0), (2, 25.0)],
                    ),
                ]
            ),
            content_type="application/xml",
        )

    async with _running_server(handler) as server:
        async with EntsoePriceProvider(_config(), base_url=_base_url(server)) as provider:
            points = list(
                await provider.fetch_prices(
                    datetime(2026, 4, 30, tzinfo=UTC),
                    datetime(2026, 4, 30, 4, tzinfo=UTC),
                )
            )

    assert [p.consumption_eur_per_kwh for p in points] == pytest.approx(
        [0.050, 0.045, 0.030, 0.025]
    )


# ----- errors ------------------------------------------------------------------


async def test_http_error_raises_fetch_error() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.Response(status=503)

    async with _running_server(handler) as server:
        async with EntsoePriceProvider(_config(), base_url=_base_url(server)) as provider:
            with pytest.raises(PriceFetchError, match="503"):
                await provider.fetch_prices(
                    datetime(2026, 4, 30, tzinfo=UTC),
                    datetime(2026, 5, 1, tzinfo=UTC),
                )


async def test_invalid_xml_raises_parse_error() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.Response(text="<<not-xml>>", content_type="application/xml")

    async with _running_server(handler) as server:
        async with EntsoePriceProvider(_config(), base_url=_base_url(server)) as provider:
            with pytest.raises(PriceParseError, match="XML"):
                await provider.fetch_prices(
                    datetime(2026, 4, 30, tzinfo=UTC),
                    datetime(2026, 5, 1, tzinfo=UTC),
                )


async def test_missing_resolution_raises_parse_error() -> None:
    bad_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Publication_MarketDocument><TimeSeries><Period>"
        "<timeInterval><start>2026-04-30T00:00Z</start><end>2026-04-30T01:00Z</end></timeInterval>"
        "<Point><position>1</position><price.amount>50.0</price.amount></Point>"
        "</Period></TimeSeries></Publication_MarketDocument>"
    )

    async def handler(_: web.Request) -> web.Response:
        return web.Response(text=bad_xml, content_type="application/xml")

    async with _running_server(handler) as server:
        async with EntsoePriceProvider(_config(), base_url=_base_url(server)) as provider:
            with pytest.raises(PriceParseError, match="resolution"):
                await provider.fetch_prices(
                    datetime(2026, 4, 30, tzinfo=UTC),
                    datetime(2026, 5, 1, tzinfo=UTC),
                )


def test_constructor_rejects_missing_api_key() -> None:
    cfg = PricesConfig.model_construct(provider=PricesProvider.ENTSOE, api_key=None)
    with pytest.raises(PriceConfigurationError, match="api_key"):
        EntsoePriceProvider(cfg)
