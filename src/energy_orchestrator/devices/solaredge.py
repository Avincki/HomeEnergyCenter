"""SolarEdge Modbus TCP client.

Reads and writes the inverter's "Active Power Limit" holding register
(``0xF001``). The orchestrator writes ``0`` to curtail the inverter and
``100`` to release it; every write is read-back-verified before returning
success.

Prerequisites on the inverter (one-time installer setup):
  * Modbus TCP enabled (SetApp -> Site Communication).
  * Advanced Power Control enabled AND committed.

Errors are mapped to the standard ``DeviceError`` hierarchy. Modbus
exceptions force the connection to be torn down so the next call
reconnects from scratch.
"""

from __future__ import annotations

import contextlib

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from energy_orchestrator.config.models import SolarEdgeConfig
from energy_orchestrator.data.models import SourceName
from energy_orchestrator.devices.base import DeviceClient, DeviceReading
from energy_orchestrator.devices.errors import (
    DeviceConnectionError,
    DeviceError,
    DeviceProtocolError,
    DeviceTimeoutError,
)
from energy_orchestrator.devices.registry import register_device

# SolarEdge "Advanced Power Control - Active Power Limit" holding register.
ACTIVE_POWER_LIMIT_REGISTER = 0xF001
OFF_PCT = 0
ON_PCT = 100


@register_device(SolarEdgeConfig)
class SolarEdgeClient(DeviceClient[SolarEdgeConfig]):
    source_name = SourceName.SOLAREDGE

    def __init__(self, config: SolarEdgeConfig) -> None:
        super().__init__(config)
        self._client: AsyncModbusTcpClient | None = None

    @property
    def _endpoint(self) -> str:
        return f"{self.config.host}:{self.config.modbus_port}/unit-{self.config.unit_id}"

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
        self._client = None

    async def _drop_connection(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    async def _ensure_connected(self) -> AsyncModbusTcpClient:
        if self._client is None:
            self._client = AsyncModbusTcpClient(
                host=self.config.host,
                port=self.config.modbus_port,
                timeout=self.config.timeout_s,
            )
        if not getattr(self._client, "connected", False):
            try:
                ok = await self._client.connect()
            except TimeoutError as e:
                await self._drop_connection()
                raise DeviceTimeoutError(f"SolarEdge connect timed out at {self._endpoint}") from e
            except ModbusException as e:
                await self._drop_connection()
                raise DeviceConnectionError(
                    f"SolarEdge connect failed at {self._endpoint}: {e}"
                ) from e
            if not ok:
                await self._drop_connection()
                raise DeviceConnectionError(f"SolarEdge could not connect at {self._endpoint}")
        return self._client

    async def read_active_power_limit(self) -> int:
        client = await self._ensure_connected()
        try:
            result = await client.read_holding_registers(
                address=ACTIVE_POWER_LIMIT_REGISTER,
                count=1,
                device_id=self.config.unit_id,
            )
        except TimeoutError as e:
            await self._drop_connection()
            raise DeviceTimeoutError(f"SolarEdge read timed out at {self._endpoint}") from e
        except ModbusException as e:
            await self._drop_connection()
            raise DeviceConnectionError(f"SolarEdge read error at {self._endpoint}: {e}") from e
        if result.isError():
            raise DeviceProtocolError(
                f"SolarEdge read returned error at {self._endpoint}: {result}"
            )
        return int(result.registers[0])

    async def set_active_power_limit(self, value: int) -> None:
        """Write the active-power-limit register and verify by read-back.

        Raises ``ValueError`` for out-of-range input (no I/O attempted).
        Raises ``DeviceProtocolError`` on write failure or read-back mismatch.
        """
        if not 0 <= value <= 100:
            raise ValueError(f"active power limit must be 0-100, got {value}")
        client = await self._ensure_connected()
        try:
            result = await client.write_register(
                address=ACTIVE_POWER_LIMIT_REGISTER,
                value=value,
                device_id=self.config.unit_id,
            )
        except TimeoutError as e:
            await self._drop_connection()
            raise DeviceTimeoutError(f"SolarEdge write timed out at {self._endpoint}") from e
        except ModbusException as e:
            await self._drop_connection()
            raise DeviceConnectionError(f"SolarEdge write error at {self._endpoint}: {e}") from e
        if result.isError():
            raise DeviceProtocolError(
                f"SolarEdge write returned error at {self._endpoint}: {result}"
            )
        actual = await self.read_active_power_limit()
        if actual != value:
            raise DeviceProtocolError(
                f"SolarEdge read-back mismatch at {self._endpoint}: "
                f"wrote {value}, read back {actual}"
            )

    async def read_data(self) -> DeviceReading | None:
        limit = await self.read_active_power_limit()
        return DeviceReading(
            device_id=self.device_id,
            data={"active_power_limit_pct": float(limit)},
            quality=1.0,
        )

    async def health_check(self) -> bool:
        try:
            await self.read_active_power_limit()
        except DeviceError:
            return False
        return True
