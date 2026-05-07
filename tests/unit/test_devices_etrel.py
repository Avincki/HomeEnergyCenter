from __future__ import annotations

import struct
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymodbus.exceptions import ModbusException
from pytest_mock import MockerFixture

from energy_orchestrator.config.models import EtrelInchConfig
from energy_orchestrator.devices import (
    DeviceConnectionError,
    DeviceProtocolError,
    DeviceTimeoutError,
    EtrelInchClient,
)


def _config() -> EtrelInchConfig:
    return EtrelInchConfig(host="192.168.1.250", modbus_port=502, unit_id=1, timeout_s=2.0)


def _f32_to_regs(value: float, *, big: bool = True) -> tuple[int, int]:
    """Encode a float32 into two Modbus registers in either word order."""
    raw = struct.pack(">f", value)
    high = int.from_bytes(raw[0:2], "big")
    low = int.from_bytes(raw[2:4], "big")
    return (high, low) if big else (low, high)


def _make_response(registers: list[int], *, is_error: bool = False) -> MagicMock:
    result = MagicMock()
    result.isError.return_value = is_error
    result.registers = registers
    return result


def _patch_modbus(
    mocker: MockerFixture,
    *,
    responses: dict[int, MagicMock] | None = None,
    default_response: MagicMock | None = None,
    read_side_effect: BaseException | None = None,
    connect_returns: bool = True,
    connect_raises: BaseException | None = None,
) -> MagicMock:
    """Patch ``AsyncModbusTcpClient`` and route reads by start address.

    ``responses`` keys are register addresses; the matching MagicMock is
    returned when the client reads that address. ``default_response`` covers
    addresses not explicitly mapped (useful for "everything succeeds" tests).
    """
    mock_cls = mocker.patch("energy_orchestrator.devices.etrel.AsyncModbusTcpClient")
    from pymodbus.client import AsyncModbusTcpClient as _Real

    mock_cls.DATATYPE = _Real.DATATYPE
    mock_cls.convert_from_registers = _Real.convert_from_registers

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

    if read_side_effect is not None:
        instance.read_holding_registers = AsyncMock(side_effect=read_side_effect)
    else:

        async def _read(*, address: int, count: int, device_id: int) -> MagicMock:
            # Diagnostic-dump paths: large sweeps at the live block (addr 0)
            # and the info block (addr 990). Tests don't care about contents —
            # return zeros so the dump succeeds silently and the field reads
            # that follow stay deterministic.
            if address == 0 and count >= 16:
                return _make_response([0] * count)
            if address == 990 and count >= 16:
                return _make_response([0] * count)
            if responses and address in responses:
                return responses[address]
            if default_response is not None:
                return default_response
            return _make_response([0] * count)

        instance.read_holding_registers = AsyncMock(side_effect=_read)

    return cast(MagicMock, instance)


def _all_ok_responses(
    *,
    status: int = 2,
    setpoint_a: float = 16.0,
    voltage_l1_v: float = 230.5,
    power_kw: float = 3.7,
    custom_max_a: int = 32,
    big_endian: bool = True,
) -> dict[int, MagicMock]:
    return {
        0: _make_response([status]),
        4: _make_response(list(_f32_to_regs(setpoint_a, big=big_endian))),
        8: _make_response(list(_f32_to_regs(voltage_l1_v, big=big_endian))),
        26: _make_response(list(_f32_to_regs(power_kw, big=big_endian))),
        1028: _make_response([custom_max_a]),
    }


# ----- read --------------------------------------------------------------------


async def test_read_data_decodes_all_fields(mocker: MockerFixture) -> None:
    instance = _patch_modbus(
        mocker,
        responses=_all_ok_responses(
            status=2, setpoint_a=16.0, voltage_l1_v=230.5, power_kw=3.7, custom_max_a=32
        ),
    )
    async with EtrelInchClient(_config()) as client:
        reading = await client.read_data()

    assert reading is not None
    assert reading.device_id == "etrel"
    assert reading.data["status_code"] == 2.0
    assert reading.data["status"] == "Charging"
    assert reading.data["setpoint_a"] == pytest.approx(16.0, rel=1e-5)
    assert reading.data["voltage_l1_v"] == pytest.approx(230.5, rel=1e-5)
    assert reading.data["power_kw"] == pytest.approx(3.7, rel=1e-5)
    assert reading.data["power_w"] == pytest.approx(3700.0, rel=1e-5)
    assert reading.data["custom_max_a"] == 32.0

    # 7 reads expected: status (cheap probe), diagnostic dump live block,
    # diagnostic dump info block, voltage, setpoint, power_total, custom_max.
    calls = instance.read_holding_registers.await_args_list
    addresses = [(c.kwargs["address"], c.kwargs["count"]) for c in calls]
    assert addresses == [
        (0, 1),
        (0, 48),
        (990, 50),
        (8, 2),
        (4, 2),
        (26, 2),
        (1028, 1),
    ]


async def test_custom_max_is_cached_after_first_read(mocker: MockerFixture) -> None:
    """Reg 1028 is installer-configured static — only fetched once per session."""
    instance = _patch_modbus(mocker, responses=_all_ok_responses())
    client = EtrelInchClient(_config())
    await client.read_data()
    await client.read_data()
    await client.read_data()
    await client.close()

    # First tick reads 1028 once; subsequent ticks must skip it.
    addresses = [call.kwargs["address"] for call in instance.read_holding_registers.await_args_list]
    assert addresses.count(1028) == 1


async def test_custom_max_retried_when_first_attempt_failed(mocker: MockerFixture) -> None:
    """If reg 1028 silently fails, we keep trying it on later ticks."""
    call_count = {"n": 0}

    async def _read(*, address: int, count: int, device_id: int) -> MagicMock:
        if address == 1028:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise TimeoutError()
            return _make_response([32])
        if address == 0:
            return _make_response([2])
        if address == 4:
            return _make_response(list(_f32_to_regs(16.0)))
        if address == 8:
            return _make_response(list(_f32_to_regs(230.5)))
        if address == 26:
            return _make_response(list(_f32_to_regs(3.7)))
        return _make_response([0] * count)

    mock_cls = mocker.patch("energy_orchestrator.devices.etrel.AsyncModbusTcpClient")
    from pymodbus.client import AsyncModbusTcpClient as _Real

    mock_cls.DATATYPE = _Real.DATATYPE
    mock_cls.convert_from_registers = _Real.convert_from_registers
    instance = mock_cls.return_value
    instance.connected = False

    async def _connect() -> bool:
        instance.connected = True
        return True

    instance.connect = AsyncMock(side_effect=_connect)
    instance.close = MagicMock()
    instance.read_holding_registers = AsyncMock(side_effect=_read)

    client = EtrelInchClient(_config())
    r1 = await client.read_data()
    r2 = await client.read_data()
    await client.close()

    # First tick: reg 1028 timed out → custom_max_a is None.
    assert r1 is not None and r1.data["custom_max_a"] is None
    # Second tick: reg 1028 retried, this time returning 32.
    assert r2 is not None and r2.data["custom_max_a"] == 32.0
    assert call_count["n"] == 2


async def test_read_data_swaps_word_order_when_voltage_implausible(
    mocker: MockerFixture,
) -> None:
    """When big-endian decode of L1 voltage falls outside the mains envelope,
    the client picks little-endian for the rest of the session — without a
    second Modbus read, since the same register bytes decode both ways."""
    read_count_at_addr_8 = {"n": 0}

    async def _read(*, address: int, count: int, device_id: int) -> MagicMock:
        if address == 0 and count >= 16:
            return _make_response([0] * count)  # diagnostic dump
        if address == 0:
            return _make_response([2])
        if address == 8:
            read_count_at_addr_8["n"] += 1
            return _make_response(list(_f32_to_regs(229.0, big=False)))
        if address == 4:
            return _make_response(list(_f32_to_regs(10.0, big=False)))
        if address == 26:
            return _make_response(list(_f32_to_regs(2.3, big=False)))
        if address == 1028:
            return _make_response([32])
        return _make_response([0] * count)

    mock_cls = mocker.patch("energy_orchestrator.devices.etrel.AsyncModbusTcpClient")
    from pymodbus.client import AsyncModbusTcpClient as _Real

    mock_cls.DATATYPE = _Real.DATATYPE
    mock_cls.convert_from_registers = _Real.convert_from_registers
    instance = mock_cls.return_value
    instance.connected = False

    async def _connect() -> bool:
        instance.connected = True
        return True

    instance.connect = AsyncMock(side_effect=_connect)
    instance.close = MagicMock()
    instance.read_holding_registers = AsyncMock(side_effect=_read)

    async with EtrelInchClient(_config()) as client:
        reading = await client.read_data()

    assert reading is not None
    assert reading.data["voltage_l1_v"] == pytest.approx(229.0, rel=1e-4)
    # Subsequent fields decode with the now-sticky little-endian order.
    assert reading.data["power_kw"] == pytest.approx(2.3, rel=1e-4)
    assert reading.data["setpoint_a"] == pytest.approx(10.0, rel=1e-4)
    # Voltage was read once — local decode handles both word orders.
    assert read_count_at_addr_8["n"] == 1


async def test_read_unknown_status_falls_back_to_label(mocker: MockerFixture) -> None:
    responses = _all_ok_responses()
    responses[0] = _make_response([42])
    _patch_modbus(mocker, responses=responses)
    async with EtrelInchClient(_config()) as client:
        reading = await client.read_data()
    assert reading is not None
    assert reading.data["status"] == "Unknown (42)"


async def test_status_read_isError_raises_protocol_error(mocker: MockerFixture) -> None:
    """First read (status) is the cheap probe; an error result here aborts."""
    responses = _all_ok_responses()
    responses[0] = _make_response([0], is_error=True)
    _patch_modbus(mocker, responses=responses)
    async with EtrelInchClient(_config()) as client:
        with pytest.raises(DeviceProtocolError, match="read returned error"):
            await client.read_data()


async def test_read_modbus_exception_raises_connection_error(
    mocker: MockerFixture,
) -> None:
    _patch_modbus(mocker, read_side_effect=ModbusException("bus error"))
    async with EtrelInchClient(_config()) as client:
        with pytest.raises(DeviceConnectionError, match="read error"):
            await client.read_data()


async def test_read_timeout_raises_timeout_error(mocker: MockerFixture) -> None:
    _patch_modbus(mocker, read_side_effect=TimeoutError())
    async with EtrelInchClient(_config()) as client:
        with pytest.raises(DeviceTimeoutError, match="read timed out"):
            await client.read_data()


# ----- connect path ------------------------------------------------------------


async def test_connect_returns_false_raises_connection_error(
    mocker: MockerFixture,
) -> None:
    _patch_modbus(mocker, connect_returns=False)
    async with EtrelInchClient(_config()) as client:
        with pytest.raises(DeviceConnectionError, match="could not connect"):
            await client.read_data()


async def test_connect_modbus_exception_raises_connection_error(
    mocker: MockerFixture,
) -> None:
    _patch_modbus(mocker, connect_raises=ModbusException("oops"))
    async with EtrelInchClient(_config()) as client:
        with pytest.raises(DeviceConnectionError):
            await client.read_data()


async def test_connect_timeout_raises_timeout_error(mocker: MockerFixture) -> None:
    _patch_modbus(mocker, connect_raises=TimeoutError())
    async with EtrelInchClient(_config()) as client:
        with pytest.raises(DeviceTimeoutError):
            await client.read_data()


# ----- health check ------------------------------------------------------------


async def test_health_check_reads_only_status_register(mocker: MockerFixture) -> None:
    """health_check is the cheap reachability probe — single 1-reg read at 0."""
    instance = _patch_modbus(mocker, responses={0: _make_response([0])})
    async with EtrelInchClient(_config()) as client:
        assert await client.health_check() is True
    addresses = [call.kwargs["address"] for call in instance.read_holding_registers.await_args_list]
    assert addresses == [0]


async def test_health_check_false_on_failure(mocker: MockerFixture) -> None:
    _patch_modbus(mocker, connect_returns=False)
    async with EtrelInchClient(_config()) as client:
        assert await client.health_check() is False


# ----- connection lifecycle ----------------------------------------------------


async def test_connection_dropped_on_modbus_error(mocker: MockerFixture) -> None:
    instance = _patch_modbus(mocker, read_side_effect=ModbusException("transient"))

    client = EtrelInchClient(_config())
    with pytest.raises(DeviceConnectionError):
        await client.read_data()

    assert client._client is None
    instance.close.assert_called()
    await client.close()
