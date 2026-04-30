from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from energy_orchestrator.config.models import (
    CarChargerConfig,
    HomeWizardDeviceConfig,
    P1MeterConfig,
    SmallSolarConfig,
)
from energy_orchestrator.devices import (
    CarChargerClient,
    DeviceConnectionError,
    DeviceProtocolError,
    HomeWizardClient,
    P1MeterClient,
    SmallSolarClient,
    UnknownDeviceTypeError,
    create_device_client,
)
from energy_orchestrator.devices.base import DeviceClient

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@asynccontextmanager
async def _running_server(handler: Handler) -> AsyncIterator[TestServer]:
    app = web.Application()
    app.router.add_get("/api/v1/data", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


def _make_config(
    config_cls: type[HomeWizardDeviceConfig], port: int, *, retry_count: int = 1
) -> HomeWizardDeviceConfig:
    kwargs: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": port,
        "retry_count": retry_count,
        "timeout_s": 2.0,
    }
    if config_cls is SmallSolarConfig:
        kwargs["peak_w"] = 2000.0
    return config_cls(**kwargs)


VARIANTS: list[tuple[type[HomeWizardClient[Any]], type[HomeWizardDeviceConfig], str]] = [
    (CarChargerClient, CarChargerConfig, "car_charger"),
    (P1MeterClient, P1MeterConfig, "p1_meter"),
    (SmallSolarClient, SmallSolarConfig, "small_solar"),
]


# ----- happy path (parametrized over all 3 variants) ---------------------------


@pytest.mark.parametrize(("client_cls", "config_cls", "expected_source"), VARIANTS)
async def test_successful_read(
    client_cls: type[HomeWizardClient[Any]],
    config_cls: type[HomeWizardDeviceConfig],
    expected_source: str,
) -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "active_power_w": 1234.5,
                "total_power_import_kwh": 1000.0,
                "total_power_export_kwh": 50.0,
                "wifi_strength": 85,  # extra — ignored
            }
        )

    async with _running_server(handler) as server:
        assert server.port is not None
        config = _make_config(config_cls, server.port)
        async with client_cls(config) as client:
            reading = await client.read_data()

    assert reading is not None
    assert reading.device_id == expected_source
    assert reading.data == {
        "active_power_w": 1234.5,
        "total_power_import_kwh": 1000.0,
        "total_power_export_kwh": 50.0,
    }
    assert reading.quality == 1.0


@pytest.mark.parametrize(("client_cls", "config_cls", "expected_source"), VARIANTS)
def test_registry_maps_each_config_to_correct_client(
    client_cls: type[HomeWizardClient[Any]],
    config_cls: type[HomeWizardDeviceConfig],
    expected_source: str,
) -> None:
    config = _make_config(config_cls, 80)
    client = create_device_client(config)
    try:
        assert isinstance(client, client_cls)
        assert str(client.source_name) == expected_source
    finally:
        # client never opened a session — no awaitable cleanup required.
        pass


# ----- normalization edge cases ------------------------------------------------


async def test_missing_active_power_returns_none() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"total_power_import_kwh": 1000.0})

    async with _running_server(handler) as server:
        assert server.port is not None
        async with CarChargerClient(_make_config(CarChargerConfig, server.port)) as client:
            reading = await client.read_data()

    assert reading is None


async def test_only_active_power_present_still_reads() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"active_power_w": 250.0})

    async with _running_server(handler) as server:
        assert server.port is not None
        async with CarChargerClient(_make_config(CarChargerConfig, server.port)) as client:
            reading = await client.read_data()

    assert reading is not None
    assert reading.data == {"active_power_w": 250.0}
    assert reading.quality == 1.0


async def test_non_numeric_field_silently_dropped() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "active_power_w": 100.0,
                "total_power_import_kwh": "not-a-number",
            }
        )

    async with _running_server(handler) as server:
        assert server.port is not None
        async with P1MeterClient(_make_config(P1MeterConfig, server.port)) as client:
            reading = await client.read_data()

    assert reading is not None
    assert "total_power_import_kwh" not in reading.data


# ----- error paths (one variant — the logic is shared) ------------------------


async def test_500_retries_and_raises_connection_error() -> None:
    counter = {"n": 0}

    async def handler(_: web.Request) -> web.Response:
        counter["n"] += 1
        return web.Response(status=500)

    async with _running_server(handler) as server:
        assert server.port is not None
        cfg = _make_config(CarChargerConfig, server.port, retry_count=3)
        async with CarChargerClient(cfg) as client:
            with pytest.raises(DeviceConnectionError, match="500"):
                await client.read_data()

    assert counter["n"] == 3


async def test_invalid_json_raises_protocol_error() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.Response(status=200, text="<html>not json</html>", content_type="text/html")

    async with _running_server(handler) as server:
        assert server.port is not None
        async with P1MeterClient(_make_config(P1MeterConfig, server.port)) as client:
            with pytest.raises(DeviceProtocolError):
                await client.read_data()


async def test_connection_refused_raises_connection_error() -> None:
    config = CarChargerConfig(host="127.0.0.1", port=1, retry_count=1, timeout_s=1.0)
    async with CarChargerClient(config) as client:
        with pytest.raises(DeviceConnectionError):
            await client.read_data()


# ----- health check ------------------------------------------------------------


async def test_health_check_true_on_success() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"active_power_w": 100.0})

    async with _running_server(handler) as server:
        assert server.port is not None
        async with SmallSolarClient(_make_config(SmallSolarConfig, server.port)) as client:
            assert await client.health_check() is True


async def test_health_check_false_on_unreachable() -> None:
    config = CarChargerConfig(host="127.0.0.1", port=1, retry_count=1, timeout_s=1.0)
    async with CarChargerClient(config) as client:
        assert await client.health_check() is False


# ----- meta --------------------------------------------------------------------


def test_homewizard_base_is_not_directly_registered() -> None:
    """The shared base must not be registered for HomeWizardDeviceConfig — only
    the three concrete subclasses are usable via the factory."""
    cfg = HomeWizardDeviceConfig(host="127.0.0.1", port=80)
    # HomeWizardDeviceConfig itself isn't a registered config type.
    with pytest.raises(UnknownDeviceTypeError):
        create_device_client(cfg)


def test_subclasses_are_concrete_subclasses_of_base() -> None:
    for cls, _, _ in VARIANTS:
        assert issubclass(cls, HomeWizardClient)
        assert issubclass(cls, DeviceClient)
