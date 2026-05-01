from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymodbus.exceptions import ModbusException
from pytest_mock import MockerFixture

from energy_orchestrator.config.models import SolarEdgeConfig
from energy_orchestrator.devices import (
    DeviceConnectionError,
    DeviceProtocolError,
    DeviceTimeoutError,
    SolarEdgeClient,
)


def _config() -> SolarEdgeConfig:
    return SolarEdgeConfig(host="127.0.0.1", modbus_port=1502, unit_id=1, timeout_s=2.0)


def _patch_modbus(
    mocker: MockerFixture,
    *,
    read_values: list[int] | None = None,
    write_is_error: bool = False,
    read_is_error: bool = False,
    connect_returns: bool = True,
    connect_raises: BaseException | None = None,
) -> MagicMock:
    """Patch ``AsyncModbusTcpClient`` and return its (single) mock instance."""
    if read_values is None:
        read_values = [100]
    mock_cls = mocker.patch("energy_orchestrator.devices.solaredge.AsyncModbusTcpClient")
    instance = mock_cls.return_value
    instance.connected = False

    if connect_raises is not None:
        instance.connect = AsyncMock(side_effect=connect_raises)
    else:

        async def _connect() -> bool:
            instance.connected = connect_returns
            return connect_returns

        instance.connect = AsyncMock(side_effect=_connect)

    instance.close = MagicMock()

    read_results = []
    for v in read_values:
        result = MagicMock()
        result.isError.return_value = read_is_error
        result.registers = [v]
        read_results.append(result)
    instance.read_holding_registers = AsyncMock(side_effect=read_results)

    write_result = MagicMock()
    write_result.isError.return_value = write_is_error
    instance.write_register = AsyncMock(return_value=write_result)

    return cast(MagicMock, instance)


# ----- read --------------------------------------------------------------------


async def test_read_data_returns_active_power_limit(mocker: MockerFixture) -> None:
    instance = _patch_modbus(mocker, read_values=[42])
    async with SolarEdgeClient(_config()) as client:
        reading = await client.read_data()

    assert reading is not None
    assert reading.device_id == "solaredge"
    assert reading.data == {"active_power_limit_pct": 42.0}
    assert reading.quality == 1.0
    instance.connect.assert_awaited_once()
    instance.read_holding_registers.assert_awaited_once_with(address=0xF001, count=1, slave=1)


async def test_read_isError_raises_protocol_error(mocker: MockerFixture) -> None:
    _patch_modbus(mocker, read_is_error=True)
    async with SolarEdgeClient(_config()) as client:
        with pytest.raises(DeviceProtocolError, match="read returned error"):
            await client.read_data()


async def test_read_modbus_exception_raises_connection_error(mocker: MockerFixture) -> None:
    instance = _patch_modbus(mocker)
    instance.read_holding_registers = AsyncMock(side_effect=ModbusException("bus error"))
    async with SolarEdgeClient(_config()) as client:
        with pytest.raises(DeviceConnectionError, match="read error"):
            await client.read_data()


async def test_read_timeout_raises_timeout_error(mocker: MockerFixture) -> None:
    instance = _patch_modbus(mocker)
    instance.read_holding_registers = AsyncMock(side_effect=TimeoutError())
    async with SolarEdgeClient(_config()) as client:
        with pytest.raises(DeviceTimeoutError, match="read timed out"):
            await client.read_data()


# ----- set_active_power_limit --------------------------------------------------


async def test_set_off_writes_zero_and_reads_back(mocker: MockerFixture) -> None:
    instance = _patch_modbus(mocker, read_values=[0])
    async with SolarEdgeClient(_config()) as client:
        await client.set_active_power_limit(0)

    instance.write_register.assert_awaited_once_with(address=0xF001, value=0, slave=1)
    instance.read_holding_registers.assert_awaited_once()


async def test_set_on_writes_hundred_and_reads_back(mocker: MockerFixture) -> None:
    instance = _patch_modbus(mocker, read_values=[100])
    async with SolarEdgeClient(_config()) as client:
        await client.set_active_power_limit(100)

    instance.write_register.assert_awaited_once_with(address=0xF001, value=100, slave=1)
    instance.read_holding_registers.assert_awaited_once()


async def test_set_invalid_value_rejected_without_io(mocker: MockerFixture) -> None:
    instance = _patch_modbus(mocker)
    async with SolarEdgeClient(_config()) as client:
        with pytest.raises(ValueError, match="0-100"):
            await client.set_active_power_limit(150)
        with pytest.raises(ValueError, match="0-100"):
            await client.set_active_power_limit(-1)

    instance.write_register.assert_not_called()


async def test_write_isError_raises_protocol_error(mocker: MockerFixture) -> None:
    _patch_modbus(mocker, write_is_error=True)
    async with SolarEdgeClient(_config()) as client:
        with pytest.raises(DeviceProtocolError, match="write returned error"):
            await client.set_active_power_limit(100)


async def test_read_back_mismatch_raises_protocol_error(mocker: MockerFixture) -> None:
    # Wrote 100, but the read after write returns 50.
    _patch_modbus(mocker, read_values=[50])
    async with SolarEdgeClient(_config()) as client:
        with pytest.raises(DeviceProtocolError, match="read-back mismatch"):
            await client.set_active_power_limit(100)


async def test_write_modbus_exception_raises_connection_error(mocker: MockerFixture) -> None:
    instance = _patch_modbus(mocker)
    instance.write_register = AsyncMock(side_effect=ModbusException("write fail"))
    async with SolarEdgeClient(_config()) as client:
        with pytest.raises(DeviceConnectionError, match="write error"):
            await client.set_active_power_limit(100)


# ----- connect path ------------------------------------------------------------


async def test_connect_returns_false_raises_connection_error(mocker: MockerFixture) -> None:
    _patch_modbus(mocker, connect_returns=False)
    async with SolarEdgeClient(_config()) as client:
        with pytest.raises(DeviceConnectionError, match="could not connect"):
            await client.read_data()


async def test_connect_modbus_exception_raises_connection_error(
    mocker: MockerFixture,
) -> None:
    _patch_modbus(mocker, connect_raises=ModbusException("oops"))
    async with SolarEdgeClient(_config()) as client:
        with pytest.raises(DeviceConnectionError):
            await client.read_data()


async def test_connect_timeout_raises_timeout_error(mocker: MockerFixture) -> None:
    _patch_modbus(mocker, connect_raises=TimeoutError())
    async with SolarEdgeClient(_config()) as client:
        with pytest.raises(DeviceTimeoutError):
            await client.read_data()


# ----- health check ------------------------------------------------------------


async def test_health_check_true_on_success(mocker: MockerFixture) -> None:
    _patch_modbus(mocker)
    async with SolarEdgeClient(_config()) as client:
        assert await client.health_check() is True


async def test_health_check_false_on_failure(mocker: MockerFixture) -> None:
    _patch_modbus(mocker, connect_returns=False)
    async with SolarEdgeClient(_config()) as client:
        assert await client.health_check() is False


# ----- connection lifecycle ----------------------------------------------------


async def test_close_closes_underlying_client(mocker: MockerFixture) -> None:
    instance = _patch_modbus(mocker)
    client = SolarEdgeClient(_config())
    await client.read_data()
    await client.close()
    instance.close.assert_called_once()


async def test_connection_reused_across_reads(mocker: MockerFixture) -> None:
    instance = _patch_modbus(mocker, read_values=[100, 100, 100])
    async with SolarEdgeClient(_config()) as client:
        await client.read_data()
        await client.read_data()
        await client.read_data()

    instance.connect.assert_awaited_once()
    assert instance.read_holding_registers.await_count == 3


async def test_connection_dropped_on_modbus_error(mocker: MockerFixture) -> None:
    instance = _patch_modbus(mocker)
    instance.read_holding_registers = AsyncMock(side_effect=ModbusException("transient"))

    client = SolarEdgeClient(_config())
    with pytest.raises(DeviceConnectionError):
        await client.read_data()

    # Connection torn down so the next call reconnects.
    assert client._client is None
    instance.close.assert_called()
    await client.close()
