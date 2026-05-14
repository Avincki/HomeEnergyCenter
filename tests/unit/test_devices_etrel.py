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
    input_responses: dict[int, MagicMock] | None = None,
    holding_responses: dict[int, MagicMock] | None = None,
    input_side_effect: BaseException | None = None,
    holding_side_effect: BaseException | None = None,
    both_side_effect: BaseException | None = None,
    connect_returns: bool = True,
    connect_raises: BaseException | None = None,
) -> MagicMock:
    """Patch ``AsyncModbusTcpClient`` and route both input + holding reads.

    ``input_responses`` / ``holding_responses`` map start addresses to the
    response the client should get for that function code. Diagnostic-dump
    sweeps (large counts at known base addresses) are auto-routed to a
    zero block so tests don't have to mock them out individually.
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

    def _make_handler(
        responses: dict[int, MagicMock] | None,
        side_effect: BaseException | None,
    ):
        if side_effect is not None or both_side_effect is not None:
            return AsyncMock(side_effect=side_effect or both_side_effect)

        async def _read(*, address: int, count: int, device_id: int) -> MagicMock:
            # Diagnostic-dump sweeps: large reads at the live block (addr 0)
            # and the info block (addr 990). Tests don't care about contents
            # so return zeros — keeps field reads deterministic afterward.
            if address == 0 and count >= 16:
                return _make_response([0] * count)
            if address == 990 and count >= 16:
                return _make_response([0] * count)
            if responses and address in responses:
                return responses[address]
            return _make_response([0] * count)

        return AsyncMock(side_effect=_read)

    instance.read_input_registers = _make_handler(input_responses, input_side_effect)
    instance.read_holding_registers = _make_handler(holding_responses, holding_side_effect)
    return cast(MagicMock, instance)


def _ok_input_responses(
    *,
    status: int = 2,
    setpoint_a: float = 16.0,
    voltage_l1_v: float = 230.5,
    current_l1_a: float = 14.2,
    power_kw: float = 3.7,
    custom_max_a: float = 32.0,
    big_endian: bool = True,
) -> dict[int, MagicMock]:
    return {
        0: _make_response([status]),
        4: _make_response(list(_f32_to_regs(setpoint_a, big=big_endian))),
        8: _make_response(list(_f32_to_regs(voltage_l1_v, big=big_endian))),
        14: _make_response(list(_f32_to_regs(current_l1_a, big=big_endian))),
        26: _make_response(list(_f32_to_regs(power_kw, big=big_endian))),
        1028: _make_response(list(_f32_to_regs(custom_max_a, big=big_endian))),
    }


def _ok_holding_responses(
    *,
    set_current_a: float = 16.0,
    big_endian: bool = True,
) -> dict[int, MagicMock]:
    return {
        8: _make_response(list(_f32_to_regs(set_current_a, big=big_endian))),
    }


# ----- read --------------------------------------------------------------------


async def test_read_data_decodes_all_fields(mocker: MockerFixture) -> None:
    instance = _patch_modbus(
        mocker,
        input_responses=_ok_input_responses(),
        holding_responses=_ok_holding_responses(set_current_a=16.0),
    )
    async with EtrelInchClient(_config()) as client:
        reading = await client.read_data()

    assert reading is not None
    assert reading.device_id == "etrel"
    assert reading.data["status_code"] == 2.0
    assert reading.data["status"] == "Charging"
    assert reading.data["setpoint_a"] == pytest.approx(16.0, rel=1e-5)
    assert reading.data["set_current_a"] == pytest.approx(16.0, rel=1e-5)
    assert reading.data["voltage_l1_v"] == pytest.approx(230.5, rel=1e-5)
    assert reading.data["current_l1_a"] == pytest.approx(14.2, rel=1e-5)
    assert reading.data["power_kw"] == pytest.approx(3.7, rel=1e-5)
    assert reading.data["power_w"] == pytest.approx(3700.0, rel=1e-5)
    assert reading.data["custom_max_a"] == 32.0

    # Status / setpoint / voltage / current / power / custom-max all hit
    # input registers; only the set-current-setpoint readback hits holding.
    in_calls = [
        (c.kwargs["address"], c.kwargs["count"])
        for c in instance.read_input_registers.await_args_list
    ]
    ho_calls = [
        (c.kwargs["address"], c.kwargs["count"])
        for c in instance.read_holding_registers.await_args_list
    ]
    # Input: status probe → diagnostic dumps (live + info) → field reads.
    assert in_calls[:7] == [
        (0, 1),
        (0, 48),
        (990, 50),
        (8, 2),
        (4, 2),
        (14, 2),
        (26, 2),
    ]
    # Custom max current is float32 (2 regs) on this firmware, not uint16.
    assert (1028, 2) in in_calls
    # Holding: diagnostic dump for both blocks + the set-current readback.
    assert (0, 48) in ho_calls
    assert (990, 50) in ho_calls
    assert (8, 2) in ho_calls


async def test_holding_set_current_failure_does_not_kill_tick(mocker: MockerFixture) -> None:
    """Holding-register reads can be blocked even when input reads work
    (Smart-E-Grid restricted firmware). The tick should continue and surface
    set_current_a as None rather than raising."""
    instance = _patch_modbus(
        mocker,
        input_responses=_ok_input_responses(),
        holding_side_effect=ModbusException("holding registers blocked"),
    )
    async with EtrelInchClient(_config()) as client:
        reading = await client.read_data()

    assert reading is not None
    assert reading.data["set_current_a"] is None
    assert reading.data["setpoint_a"] == pytest.approx(16.0, rel=1e-5)
    # Holding reads were attempted (dump) but the field read also failed cleanly.
    assert instance.read_holding_registers.await_count >= 1


async def test_custom_max_is_cached_after_first_read(mocker: MockerFixture) -> None:
    """Reg 1028 (input) is installer-configured static — fetched once per session."""
    instance = _patch_modbus(
        mocker,
        input_responses=_ok_input_responses(),
        holding_responses=_ok_holding_responses(),
    )
    client = EtrelInchClient(_config())
    await client.read_data()
    await client.read_data()
    await client.read_data()
    await client.close()

    addresses = [c.kwargs["address"] for c in instance.read_input_registers.await_args_list]
    assert addresses.count(1028) == 1


async def test_custom_max_retried_when_first_attempt_failed(mocker: MockerFixture) -> None:
    """If reg 1028 silently fails, the next tick retries."""
    call_count = {"n": 0}

    async def _input_read(*, address: int, count: int, device_id: int) -> MagicMock:
        if address == 0 and count >= 16:
            return _make_response([0] * count)
        if address == 990 and count >= 16:
            return _make_response([0] * count)
        if address == 1028:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise TimeoutError()
            return _make_response(list(_f32_to_regs(32.0)))
        if address == 0:
            return _make_response([2])
        if address == 4:
            return _make_response(list(_f32_to_regs(16.0)))
        if address == 8:
            return _make_response(list(_f32_to_regs(230.5)))
        if address == 14:
            return _make_response(list(_f32_to_regs(14.2)))
        if address == 26:
            return _make_response(list(_f32_to_regs(3.7)))
        return _make_response([0] * count)

    async def _holding_read(*, address: int, count: int, device_id: int) -> MagicMock:
        if address == 0 and count >= 16:
            return _make_response([0] * count)
        if address == 990 and count >= 16:
            return _make_response([0] * count)
        if address == 8:
            return _make_response(list(_f32_to_regs(16.0)))
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
    instance.read_input_registers = AsyncMock(side_effect=_input_read)
    instance.read_holding_registers = AsyncMock(side_effect=_holding_read)

    client = EtrelInchClient(_config())
    r1 = await client.read_data()
    r2 = await client.read_data()
    await client.close()

    assert r1 is not None and r1.data["custom_max_a"] is None
    assert r2 is not None and r2.data["custom_max_a"] == 32.0
    assert call_count["n"] == 2


async def test_read_data_swaps_word_order_when_voltage_implausible(
    mocker: MockerFixture,
) -> None:
    """When big-endian decode of L1 voltage falls outside the mains envelope,
    the client picks little-endian for the rest of the session — without a
    second Modbus read, since the same register bytes decode both ways."""
    read_count_at_addr_8 = {"n": 0}

    async def _input_read(*, address: int, count: int, device_id: int) -> MagicMock:
        if address == 0 and count >= 16:
            return _make_response([0] * count)
        if address == 990 and count >= 16:
            return _make_response([0] * count)
        if address == 0:
            return _make_response([2])
        if address == 8:
            read_count_at_addr_8["n"] += 1
            return _make_response(list(_f32_to_regs(229.0, big=False)))
        if address == 4:
            return _make_response(list(_f32_to_regs(10.0, big=False)))
        if address == 14:
            return _make_response(list(_f32_to_regs(8.5, big=False)))
        if address == 26:
            return _make_response(list(_f32_to_regs(2.3, big=False)))
        if address == 1028:
            return _make_response(list(_f32_to_regs(32.0, big=False)))
        return _make_response([0] * count)

    async def _holding_read(*, address: int, count: int, device_id: int) -> MagicMock:
        if address == 0 and count >= 16:
            return _make_response([0] * count)
        if address == 990 and count >= 16:
            return _make_response([0] * count)
        if address == 8:
            return _make_response(list(_f32_to_regs(10.0, big=False)))
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
    instance.read_input_registers = AsyncMock(side_effect=_input_read)
    instance.read_holding_registers = AsyncMock(side_effect=_holding_read)

    async with EtrelInchClient(_config()) as client:
        reading = await client.read_data()

    assert reading is not None
    assert reading.data["voltage_l1_v"] == pytest.approx(229.0, rel=1e-4)
    assert reading.data["power_kw"] == pytest.approx(2.3, rel=1e-4)
    assert reading.data["setpoint_a"] == pytest.approx(10.0, rel=1e-4)
    # Voltage was read once — local decode handles both word orders.
    assert read_count_at_addr_8["n"] == 1


async def test_read_unknown_status_falls_back_to_label(mocker: MockerFixture) -> None:
    responses = _ok_input_responses()
    responses[0] = _make_response([42])
    _patch_modbus(
        mocker,
        input_responses=responses,
        holding_responses=_ok_holding_responses(),
    )
    async with EtrelInchClient(_config()) as client:
        reading = await client.read_data()
    assert reading is not None
    assert reading.data["status"] == "Unknown (42)"


async def test_status_read_isError_raises_protocol_error(mocker: MockerFixture) -> None:
    """Status (the cheap probe) is on input registers; an error result aborts."""
    responses = _ok_input_responses()
    responses[0] = _make_response([0], is_error=True)
    _patch_modbus(
        mocker,
        input_responses=responses,
        holding_responses=_ok_holding_responses(),
    )
    async with EtrelInchClient(_config()) as client:
        with pytest.raises(DeviceProtocolError, match="read returned error"):
            await client.read_data()


async def test_read_modbus_exception_raises_connection_error(
    mocker: MockerFixture,
) -> None:
    _patch_modbus(mocker, both_side_effect=ModbusException("bus error"))
    async with EtrelInchClient(_config()) as client:
        with pytest.raises(DeviceConnectionError, match="read error"):
            await client.read_data()


async def test_read_timeout_raises_timeout_error(mocker: MockerFixture) -> None:
    _patch_modbus(mocker, both_side_effect=TimeoutError())
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


async def test_health_check_reads_only_status_input_register(mocker: MockerFixture) -> None:
    """health_check is the cheap reachability probe — single 1-reg input read at 0."""
    instance = _patch_modbus(mocker, input_responses={0: _make_response([0])})
    async with EtrelInchClient(_config()) as client:
        assert await client.health_check() is True
    in_calls = [
        (c.kwargs["address"], c.kwargs["count"])
        for c in instance.read_input_registers.await_args_list
    ]
    assert in_calls == [(0, 1)]
    assert instance.read_holding_registers.await_count == 0


async def test_health_check_false_on_failure(mocker: MockerFixture) -> None:
    _patch_modbus(mocker, connect_returns=False)
    async with EtrelInchClient(_config()) as client:
        assert await client.health_check() is False


# ----- connection lifecycle ----------------------------------------------------


async def test_connection_dropped_on_modbus_error(mocker: MockerFixture) -> None:
    instance = _patch_modbus(mocker, both_side_effect=ModbusException("transient"))

    client = EtrelInchClient(_config())
    with pytest.raises(DeviceConnectionError):
        await client.read_data()

    assert client._client is None
    instance.close.assert_called()
    await client.close()


# ----- pause / release semantic wrappers ---------------------------------------


async def test_pause_writes_zero_amps(mocker: MockerFixture) -> None:
    """``pause()`` is a thin wrapper for ``set_charging_current_a(0.0)`` —
    the rule engine calls it instead of the magic-value form, but the wire
    behaviour must stay identical so existing diagnostics and write-path
    safety nets still apply."""
    client = EtrelInchClient(_config())
    set_current = mocker.patch.object(
        client, "set_charging_current_a", new=AsyncMock()
    )
    await client.pause()
    set_current.assert_awaited_once_with(0.0)


async def test_release_writes_requested_amps(mocker: MockerFixture) -> None:
    """``release(amps)`` mirrors ``pause()`` — symmetric naming for rule
    code, no extra logic on the write itself."""
    client = EtrelInchClient(_config())
    set_current = mocker.patch.object(
        client, "set_charging_current_a", new=AsyncMock()
    )
    await client.release(8.0)
    set_current.assert_awaited_once_with(8.0)


async def test_release_does_not_clamp_amps_caller_owns_safety(
    mocker: MockerFixture,
) -> None:
    """Per the docstring contract, ``release()`` does not enforce the 16 A
    cap — that's the API/UI/JS layers' job. If a rule passes 30 A, that's
    the rule's bug; the wrapper should still forward verbatim so the
    real failure shows up at the documented choke point."""
    client = EtrelInchClient(_config())
    set_current = mocker.patch.object(
        client, "set_charging_current_a", new=AsyncMock()
    )
    await client.release(30.0)
    set_current.assert_awaited_once_with(30.0)
