"""Etrel INCH Home/Pro EV charger Modbus TCP client.

Reads connector-1 real-time values (status, target current setpoint, L1
voltage, active power total) plus the installer-configured custom max
current (reg 1028, cached once). Float32 values use big-endian word order
per the Etrel docs; we sanity-check that against L1 voltage and fall back
to little-endian if not.

Reads are issued **per field** rather than as one block so that a silent
firmware-specific gap on a single register doesn't poison the whole
reading, and so failure logs pinpoint exactly which address didn't
respond. We also pass ``retries=1`` to pymodbus — the default 3 makes
each silent timeout cost ~15 s, well over the 5 s tick budget.

Read-only for now; the write path (set/cancel current setpoint, pause/
stop) is intentionally deferred until the official register addresses
from the Etrel XLSX are wired in alongside the control loop.

References (registers, port 502, unit ID 1):
  *  0          uint16   connector status (0=Available .. 8=Faulted)
  *  4..5       float32  target current setpoint (A) — read-back of override
  *  8..9       float32  L1 voltage (V) — endianness sanity check (~230 V)
  * 26..27      float32  active power total (kW) — primary "what's drawn"
  *  1028      uint16   custom max current (A) — installer ceiling, cached
"""

from __future__ import annotations

import contextlib
from typing import Any

import structlog
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from energy_orchestrator.config.models import EtrelInchConfig
from energy_orchestrator.data.models import SourceName
from energy_orchestrator.devices.base import DeviceClient, DeviceReading
from energy_orchestrator.devices.errors import (
    DeviceConnectionError,
    DeviceError,
    DeviceProtocolError,
    DeviceTimeoutError,
)
from energy_orchestrator.devices.registry import register_device

logger = structlog.stdlib.get_logger(__name__)

# Connector-1 registers we read live every tick.
_REG_STATUS = 0  # uint16
_REG_SETPOINT_A = 4  # float32 (2 regs)
_REG_VOLTAGE_L1 = 8  # float32 (2 regs)
_REG_POWER_TOTAL_KW = 26  # float32 (2 regs)
# Installer-configured ceiling — static, cached after first successful read.
_REG_CUSTOM_MAX_CURRENT_A = 1028  # uint16

# Plausible L1-voltage envelope used for the endianness sanity check —
# anything outside is taken as evidence of swapped word order. Float32 with
# the wrong word order produces values like 1e+38 or 1e-43, which are
# trivially excluded by this range.
_VOLTAGE_MIN_V = 80.0
_VOLTAGE_MAX_V = 300.0

# Override pymodbus's default 3 retries — on this LAN a silent timeout
# really means "no response", and tripling our wall-clock latency for it
# blows the 5 s tick budget without changing the outcome.
_PYMODBUS_RETRIES = 1

_STATUS_LABELS: dict[int, str] = {
    0: "Available",
    1: "Plugged",
    2: "Charging",
    3: "Suspended (EVSE)",
    4: "Suspended (EV)",
    5: "Finishing",
    6: "Reserved",
    7: "Unavailable",
    8: "Faulted",
}


def _status_label(code: int) -> str:
    return _STATUS_LABELS.get(code, f"Unknown ({code})")


@register_device(EtrelInchConfig)
class EtrelInchClient(DeviceClient[EtrelInchConfig]):
    source_name = SourceName.ETREL

    def __init__(self, config: EtrelInchConfig) -> None:
        super().__init__(config)
        self._client: AsyncModbusTcpClient | None = None
        # Detected on each successful tick from L1 voltage. Defaults to the
        # documented ``"big"`` so the very first decode has a sane guess.
        self._word_order: str = "big"
        # Installer-configured ceiling; cached after the first successful
        # read since it doesn't change at runtime. ``None`` means "haven't
        # successfully read it yet".
        self._custom_max_a: int | None = None
        # One-shot register dump triggered on the first successful read. The
        # documented Etrel register map has shifted between firmware versions,
        # so when our decoded values look implausible we want a single full
        # snapshot in the log to map the actual layout — not a per-tick spam.
        self._dump_done: bool = False

    @property
    def _endpoint(self) -> str:
        return f"{self.config.host}:{self.config.modbus_port}/unit-{self.config.unit_id}"

    def _log_ctx(self, address: int, count: int) -> dict[str, Any]:
        return {
            "host": self.config.host,
            "modbus_port": self.config.modbus_port,
            "unit_id": self.config.unit_id,
            "address": address,
            "count": count,
        }

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
                retries=_PYMODBUS_RETRIES,
            )
        if not getattr(self._client, "connected", False):
            try:
                ok = await self._client.connect()
            except TimeoutError as e:
                await self._drop_connection()
                raise DeviceTimeoutError(f"Etrel connect timed out at {self._endpoint}") from e
            except ModbusException as e:
                await self._drop_connection()
                raise DeviceConnectionError(
                    f"Etrel connect failed at {self._endpoint}: {e}"
                ) from e
            if not ok:
                await self._drop_connection()
                raise DeviceConnectionError(f"Etrel could not connect at {self._endpoint}")
        return self._client

    async def _read_registers(self, address: int, count: int, *, label: str) -> list[int]:
        """One holding-register read with structured failure logs.

        ``label`` identifies the field this read was for so the failure log
        is immediately readable (``status`` / ``setpoint_a`` / etc.) without
        needing to look up addresses. The actual frame details land in the
        bound context for the curious / when escalating.
        """
        client = await self._ensure_connected()
        ctx = self._log_ctx(address, count)
        try:
            result = await client.read_holding_registers(
                address=address,
                count=count,
                device_id=self.config.unit_id,
            )
        except TimeoutError as e:
            await self._drop_connection()
            logger.warning("etrel read timeout", field=label, **ctx)
            raise DeviceTimeoutError(
                f"Etrel read timed out at {self._endpoint} ({label} addr={address} count={count})"
            ) from e
        except ModbusException as e:
            await self._drop_connection()
            logger.warning("etrel read modbus error", field=label, error=str(e), **ctx)
            raise DeviceConnectionError(
                f"Etrel read error at {self._endpoint} ({label} addr={address} count={count}): {e}"
            ) from e
        if result.isError():
            logger.warning("etrel read returned error result", field=label, result=str(result), **ctx)
            raise DeviceProtocolError(
                f"Etrel read returned error at {self._endpoint} "
                f"({label} addr={address} count={count}): {result}"
            )
        return list(result.registers)

    async def _read_uint16(self, address: int, *, label: str) -> int:
        regs = await self._read_registers(address, 1, label=label)
        return int(regs[0])

    async def _read_float32(self, address: int, *, label: str) -> float:
        regs = await self._read_registers(address, 2, label=label)
        return float(
            AsyncModbusTcpClient.convert_from_registers(
                regs,
                AsyncModbusTcpClient.DATATYPE.FLOAT32,
                word_order=self._word_order,
            )
        )

    def _looks_like_voltage(self, v: float) -> bool:
        return _VOLTAGE_MIN_V <= v <= _VOLTAGE_MAX_V

    def _decode_pair(self, regs: list[int], offset: int, *, word_order: str) -> float:
        """Decode two registers at ``offset`` as float32 in the given order."""
        return float(
            AsyncModbusTcpClient.convert_from_registers(
                regs[offset : offset + 2],
                AsyncModbusTcpClient.DATATYPE.FLOAT32,
                word_order=word_order,
            )
        )

    async def _diagnostic_dump(self) -> None:
        """Read the connector-1 live block and the device-info block, then
        log both with raw values + float32 interpretations in both word orders.

        Used once per session when the documented register map doesn't decode
        plausibly — different Etrel firmware versions have shipped with
        different layouts, and the only way to identify the real one without
        the per-firmware XLSX is to eyeball a full dump for values that match
        ground truth (mains voltage ≈ 230 V, firmware string ≥ 7.1.1, …).

        Live and info blocks are read independently so a Smart-E-Grid setup
        that exposes one but not the other still produces useful output.
        """
        if self._dump_done:
            return
        # Mark before the reads so a failure doesn't queue infinite retries.
        self._dump_done = True

        try:
            live_regs = await self._read_registers(0, 48, label="diagnostic_dump_live")
        except DeviceError:
            live_regs = None

        try:
            info_regs = await self._read_registers(990, 50, label="diagnostic_dump_info")
        except DeviceError:
            info_regs = None

        if live_regs is None and info_regs is None:
            logger.warning("etrel diagnostic dump unavailable — both blocks failed")
            return

        lines = [
            f"etrel register dump (address: raw_uint16 | float32_big | float32_little) "
            f"host={self.config.host} unit_id={self.config.unit_id}",
        ]
        if live_regs is not None:
            lines.append("  -- connector 1 live block (regs 0..47) --")
            self._format_block(lines, live_regs, base_address=0)
        else:
            lines.append("  -- connector 1 live block: read failed --")

        if info_regs is not None:
            lines.append("  -- device info block (regs 990..1039) --")
            # ASCII decode helper for the string regs (serial/model/HW ver/SW
            # ver) — Etrel packs two ASCII chars per register big-endian.
            ascii_text = self._registers_to_ascii(info_regs)
            self._format_block(lines, info_regs, base_address=990)
            lines.append(f"  -- info block as ASCII: {ascii_text!r}")
        else:
            lines.append("  -- device info block: read failed --")

        # Single multi-line log entry — keeps the dump grouped in the log
        # viewer instead of interleaved with subsequent ticks.
        logger.info("\n".join(lines))

    def _format_block(self, lines: list[str], regs: list[int], *, base_address: int) -> None:
        for i, raw in enumerate(regs):
            addr = base_address + i
            entry = f"  reg {addr:4d}: 0x{raw:04x} ({raw:5d})"
            if i + 1 < len(regs):
                f_big = self._decode_pair(regs, i, word_order="big")
                f_lil = self._decode_pair(regs, i, word_order="little")
                entry += f" | f32_big={f_big:.4g} | f32_lil={f_lil:.4g}"
            lines.append(entry)

    @staticmethod
    def _registers_to_ascii(regs: list[int]) -> str:
        """Decode a register block as 16-bit big-endian ASCII pairs.

        Etrel string registers (serial, model, firmware version) pack two
        printable ASCII chars per register, high byte first. Non-printable
        bytes are rendered as ``.`` so the readable text stands out.
        """
        out: list[str] = []
        for r in regs:
            for b in (r >> 8 & 0xFF, r & 0xFF):
                out.append(chr(b) if 32 <= b < 127 else ".")
        return "".join(out)

    async def read_data(self) -> DeviceReading | None:
        # Cheapest read first — failure here means the device isn't talking
        # at all (bad unit ID, Modbus disabled, …) and there's no point
        # spending more round-trips on the rest of the fields.
        status_code = await self._read_uint16(_REG_STATUS, label="status")

        # One-shot diagnostic snapshot of the whole connector-1 block on the
        # first successful tick. Lets us identify a shifted register layout
        # by eyeballing the dump — happens once, not per tick.
        await self._diagnostic_dump()

        # Voltage probe doubles as endianness check. Read the registers once,
        # decode in both orders locally, pick the plausible one (and make it
        # sticky for the session). Critically, we do NOT flip per tick — a
        # stuck-implausible voltage just gets logged once on the dump above
        # and we move on with whatever decode we picked.
        v_regs = await self._read_registers(_REG_VOLTAGE_L1, 2, label="voltage_l1_v")
        v_big = self._decode_pair(v_regs, 0, word_order="big")
        v_lil = self._decode_pair(v_regs, 0, word_order="little")
        if self._looks_like_voltage(v_big):
            self._word_order = "big"
            voltage_l1_v = v_big
        elif self._looks_like_voltage(v_lil):
            self._word_order = "little"
            voltage_l1_v = v_lil
        else:
            # Stick with the previously-selected order; consumers see whatever
            # that decoded to. The diagnostic dump above has the raw bytes for
            # offline mapping, so no need to flip-flop per tick.
            voltage_l1_v = v_big if self._word_order == "big" else v_lil

        setpoint_a = await self._read_float32(_REG_SETPOINT_A, label="setpoint_a")
        power_kw = await self._read_float32(_REG_POWER_TOTAL_KW, label="power_kw")
        power_w = power_kw * 1000.0

        # Cache the installer ceiling on the first successful tick — re-read
        # only if it never came through, so a transient gap on the static
        # register doesn't permanently mask it.
        if self._custom_max_a is None:
            try:
                self._custom_max_a = await self._read_uint16(
                    _REG_CUSTOM_MAX_CURRENT_A, label="custom_max_a"
                )
                logger.info("etrel custom max current cached", custom_max_a=self._custom_max_a)
            except DeviceError:
                # Already logged by _read_registers; leave cache empty for next tick.
                pass

        return DeviceReading(
            device_id=self.device_id,
            data={
                "status_code": float(status_code),
                "status": _status_label(status_code),
                "setpoint_a": setpoint_a,
                "voltage_l1_v": voltage_l1_v,
                "power_kw": power_kw,
                "power_w": power_w,
                "custom_max_a": (
                    float(self._custom_max_a) if self._custom_max_a is not None else None
                ),
            },
            quality=1.0,
        )

    async def health_check(self) -> bool:
        try:
            await self._read_uint16(_REG_STATUS, label="health_check")
        except DeviceError:
            return False
        return True
