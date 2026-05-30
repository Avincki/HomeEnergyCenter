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
from typing import Any

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

# ─────────────────────────── TEMPORARY (remove me) ───────────────────────────
# ⚠️  ONE-SHOT Advanced Power Control enable/commit — STOP-GAP, REMOVE LATER.
#
# The active-power-limit register (0xF001) is only *enforced* when the inverter
# has "Advanced Power Control" enabled AND committed. If it isn't, writes to
# 0xF001 are accepted and read-back-verified but the panels keep producing
# ("accepted but ignored"). The proper fix is for the installer to enable +
# commit Advanced Power Control in SetApp.
#
# Until that is done, ``ensure_advanced_power_control_enabled()`` below enables
# it over Modbus and commits once. Committing writes to the inverter's
# non-volatile flash, which has limited write endurance, so we ONLY commit when
# the enable flag actually reads back disabled.
#
# DELETE these two constants and ``ensure_advanced_power_control_enabled()``
# (and its call site in the orchestrator) once SetApp has APC enabled+committed.
ADVANCED_PWR_CONTROL_EN_REGISTER = 0xF142  # INT32, two registers, 0=off / 1=on
COMMIT_POWER_CONTROL_REGISTER = 0xF100  # write 1 to persist power-control settings
# ──────────────────────────────────────────────────────────────────────────────


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

    # ─────────────────────────── TEMPORARY (remove me) ──────────────────────
    async def ensure_advanced_power_control_enabled(self) -> dict[str, Any]:
        """STOP-GAP one-shot: enable + commit Advanced Power Control.

        ⚠️ REMOVE once the installer has enabled & committed Advanced Power
        Control in SetApp — see the warning block above the register constants.

        Reads ``AdvancedPwrControlEn`` (0xF142, INT32). If already enabled,
        does nothing and reports ``already_enabled``. If disabled, writes 1
        (LSW first), then writes the commit register (0xF100) to persist to the
        inverter's non-volatile flash, and re-reads to confirm. We only commit
        when the flag reads back disabled, to avoid needless flash wear.

        Never raises: returns a structured result with ``error`` set on any I/O
        failure so the operator probe reports it instead of surfacing a 500.
        """

        def _result(
            *, already: bool, enabled_now: bool, committed: bool, error: str | None
        ) -> dict[str, Any]:
            return {
                "already_enabled": already,
                "enabled_now": enabled_now,
                "committed": committed,
                "error": error,
            }

        try:
            client = await self._ensure_connected()
            read = await client.read_holding_registers(
                address=ADVANCED_PWR_CONTROL_EN_REGISTER,
                count=2,
                device_id=self.config.unit_id,
            )
            if read.isError():
                return _result(
                    already=False, enabled_now=False, committed=False,
                    error=f"enable-flag read error: {read}",
                )
            if read.registers[0] != 0 or read.registers[1] != 0:
                return _result(already=True, enabled_now=True, committed=False, error=None)

            # Disabled → enable (32-bit value 1 as LSW, MSW) then commit.
            wr = await client.write_registers(
                address=ADVANCED_PWR_CONTROL_EN_REGISTER,
                values=[1, 0],
                device_id=self.config.unit_id,
            )
            if wr.isError():
                return _result(
                    already=False, enabled_now=False, committed=False,
                    error=f"enable write error: {wr}",
                )
            commit = await client.write_register(
                address=COMMIT_POWER_CONTROL_REGISTER,
                value=1,
                device_id=self.config.unit_id,
            )
            if commit.isError():
                return _result(
                    already=False, enabled_now=False, committed=False,
                    error=f"commit write error: {commit}",
                )
            check = await client.read_holding_registers(
                address=ADVANCED_PWR_CONTROL_EN_REGISTER,
                count=2,
                device_id=self.config.unit_id,
            )
            enabled_now = not check.isError() and (
                check.registers[0] != 0 or check.registers[1] != 0
            )
            return _result(
                already=False, enabled_now=enabled_now, committed=True, error=None
            )
        except TimeoutError as e:
            await self._drop_connection()
            return _result(
                already=False, enabled_now=False, committed=False, error=f"timeout: {e}"
            )
        except ModbusException as e:
            await self._drop_connection()
            return _result(
                already=False, enabled_now=False, committed=False, error=f"modbus: {e}"
            )

    # ─────────────────────────── end TEMPORARY ───────────────────────────────

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
