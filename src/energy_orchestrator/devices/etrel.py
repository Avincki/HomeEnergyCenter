"""Etrel INCH Home/Pro EV charger Modbus TCP client.

Etrel exposes data in **two distinct address spaces** with different
function codes — getting this wrong is the difference between reading
real telemetry and reading all-zero garbage:

* **Input registers** (read via FC 0x04 / ``read_input_registers``):
  read-only telemetry — connector status, voltages, currents, active
  power, energy, EV info. This is where the live data lives.

* **Holding registers** (read via FC 0x03 / ``read_holding_registers``,
  written via FC 0x06 / 0x10): write-side controls — set current
  setpoint, set power setpoint, release/cancel, pause. Reading a
  holding register returns the last value written to it (so the
  set-current setpoint reads back as the active write-side limit).

Both address spaces use the same numeric addresses (0, 4, 8, ...),
which is why a function-code mix-up returns plausible-looking-but-wrong
values: ``read_holding_registers(8)`` returns the write-side current
setpoint while ``read_input_registers(8)`` returns the L1 voltage. Both
read OK; both decode as float32; only one is what you wanted.

Float32 values use big-endian word order per the Etrel docs; we
sanity-check that against L1 voltage (~230 V) and fall back to
little-endian if not.

Reads are issued **per field** rather than as one block so that a
silent firmware-specific gap on a single register doesn't poison the
whole reading, and so failure logs pinpoint exactly which address
didn't respond. We also pass ``retries=1`` to pymodbus — the default 3
makes each silent timeout cost ~15 s, well over the 5 s tick budget.

Read-only for now; the write path (set/cancel current setpoint, pause/
stop) is intentionally deferred until the orchestrator has a control
loop that needs it.

References (port 502, configurable unit ID, default 1):

  Input registers (telemetry):
    *   0          uint16   connector status (0=Available .. 8=Faulted)
    *   4..5       float32  target current applied (A) — read-back of
                            the active limit, regardless of source
                            (Modbus override / Sonnen backend / EV cap)
    *   8..9       float32  L1 voltage (V) — endianness sanity check
    *  14..15      float32  L1 current (A)
    *  26..27      float32  active power total (kW)
    *  1028..1029  float32  custom max current (A) — installer ceiling
                            (Etrel docs called this uint16; on this
                            firmware it's actually a float32 — the raw
                            uint16 readback was 0x4180, which is exactly
                            the high half of IEEE 754 16.0 BE)

  Holding registers (write-side, also readable for verification):
    *   8..9       float32  set current setpoint (write target & readback)

Source: Etrel KB (Modbus Communication with INCH products) plus the
Home Assistant community thread that reverse-engineered the input-vs-
holding split for the Sonnen Smart-E-Grid variant.
"""

from __future__ import annotations

import asyncio
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

# Function-code identifiers — used purely for log breadcrumbs so a failure
# context immediately tells us which Modbus address space was being used.
_FC_INPUT = "input"
_FC_HOLDING = "holding"

# Input register offsets (telemetry, read-only).
_IN_STATUS = 0  # uint16
_IN_TARGET_CURRENT_A = 4  # float32 (2 regs)
_IN_VOLTAGE_L1 = 8  # float32 (2 regs)
_IN_CURRENT_L1 = 14  # float32 (2 regs)
_IN_POWER_TOTAL_KW = 26  # float32 (2 regs)
# Installer-configured ceiling — static, cached after first successful read.
# Doc says uint16 but the firmware encodes this as float32 (regs 1028..1029).
_IN_CUSTOM_MAX_CURRENT_A = 1028  # float32 (2 regs)

# Holding register offsets (write-side, also readable for verification).
_HOLD_SET_CURRENT_A = 8  # float32 (2 regs) — write target

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

# Threshold for "did this float-valued setpoint actually change". The setpoints
# are configured/written values (not analog measurements), so anything above
# this is a real change rather than encoding/decoding precision noise.
_SETPOINT_NOISE_A = 0.05

# How close a holding-register pair's float32 decode must be to the active
# setpoint to flag it as a candidate for the Sonnen cluster-limit register.
# Wide enough to absorb transient mid-write inconsistency, narrow enough to
# avoid false matches against unrelated float-shaped register pairs.
_CLUSTER_MATCH_TOLERANCE_A = 0.5

# Span of holding registers scanned per tick when looking for the register
# Sonnen's cluster channel writes to. 48 covers the connector-1 live block
# (where every documented Etrel write target lives) at one extra Modbus
# round-trip per tick — cheap on LAN.
_CLUSTER_SCAN_SPAN = 48

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
        # Installer-configured ceiling (float32 amps); cached after the
        # first successful read since it doesn't change at runtime.
        # ``None`` means "haven't successfully read it yet".
        self._custom_max_a: float | None = None
        # One-shot register dump triggered on the first successful read,
        # covering both input AND holding address spaces so a future
        # function-code mismatch is debuggable from a single log line.
        self._dump_done: bool = False
        # Previous-tick values used for change-detection logging. Kept on the
        # instance so consecutive ticks can emit "X went A → B" events only
        # when something actually moved, instead of spamming every tick.
        self._prev_status_code: int | None = None
        self._prev_setpoint_a: float | None = None
        self._prev_set_current_a: float | None = None
        self._prev_diverged: bool | None = None
        # Serializes the orchestrator's read tick against ad-hoc writes from
        # the API. The Etrel firmware on this unit is unreliable when more
        # than one Modbus TCP connection is open at once — a fresh client
        # could TCP-connect successfully but get no PDU responses while the
        # tick-loop's persistent client served reads fine. Routing every
        # call through the same client and lock eliminates both contention
        # vectors (multiple connections, interleaved transactions on one
        # connection).
        self._comm_lock: asyncio.Lock = asyncio.Lock()

    @property
    def _endpoint(self) -> str:
        return f"{self.config.host}:{self.config.modbus_port}/unit-{self.config.unit_id}"

    def _log_ctx(self, fc: str, address: int, count: int) -> dict[str, Any]:
        return {
            "host": self.config.host,
            "modbus_port": self.config.modbus_port,
            "unit_id": self.config.unit_id,
            "fc": fc,
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

    async def _read(
        self, fc: str, address: int, count: int, *, label: str
    ) -> list[int]:
        """One Modbus read with structured failure logs.

        ``fc`` is "input" (FC 0x04 — read-only telemetry) or "holding" (FC
        0x03 — write-back of holding registers). The function code is
        bound into both the failure logs and the raised exception so a
        future function-code mix-up is immediately obvious.
        """
        client = await self._ensure_connected()
        ctx = self._log_ctx(fc, address, count)
        try:
            if fc == _FC_INPUT:
                result = await client.read_input_registers(
                    address=address,
                    count=count,
                    device_id=self.config.unit_id,
                )
            else:
                result = await client.read_holding_registers(
                    address=address,
                    count=count,
                    device_id=self.config.unit_id,
                )
        except TimeoutError as e:
            await self._drop_connection()
            logger.warning("etrel read timeout", field=label, **ctx)
            raise DeviceTimeoutError(
                f"Etrel read timed out at {self._endpoint} "
                f"({label} fc={fc} addr={address} count={count})"
            ) from e
        except ModbusException as e:
            await self._drop_connection()
            logger.warning("etrel read modbus error", field=label, error=str(e), **ctx)
            raise DeviceConnectionError(
                f"Etrel read error at {self._endpoint} "
                f"({label} fc={fc} addr={address} count={count}): {e}"
            ) from e
        if result.isError():
            logger.warning(
                "etrel read returned error result", field=label, result=str(result), **ctx
            )
            raise DeviceProtocolError(
                f"Etrel read returned error at {self._endpoint} "
                f"({label} fc={fc} addr={address} count={count}): {result}"
            )
        return list(result.registers)

    async def _read_input_uint16(self, address: int, *, label: str) -> int:
        regs = await self._read(_FC_INPUT, address, 1, label=label)
        return int(regs[0])

    async def _read_input_float32(self, address: int, *, label: str) -> float:
        regs = await self._read(_FC_INPUT, address, 2, label=label)
        return self._decode_pair(regs, 0, word_order=self._word_order)

    async def _read_holding_float32(self, address: int, *, label: str) -> float:
        regs = await self._read(_FC_HOLDING, address, 2, label=label)
        return self._decode_pair(regs, 0, word_order=self._word_order)

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
        """Read the connector-1 live block + device-info block for **both**
        function codes and log each register's raw value plus float32
        interpretations in both word orders.

        Used once per session. The two-function-code dump means a future
        layout shift OR function-code mix-up is immediately diagnosable
        from a single log entry — without it we'd burn a debugging cycle
        figuring out which address space the Etrel is actually populating.
        """
        if self._dump_done:
            return
        # Mark before the reads so a failure doesn't queue infinite retries.
        self._dump_done = True

        async def _safe_read(fc: str, address: int, count: int, label: str) -> list[int] | None:
            try:
                return await self._read(fc, address, count, label=label)
            except DeviceError:
                return None

        in_live = await _safe_read(_FC_INPUT, 0, 48, "diagnostic_dump_input_live")
        in_info = await _safe_read(_FC_INPUT, 990, 50, "diagnostic_dump_input_info")
        ho_live = await _safe_read(_FC_HOLDING, 0, 48, "diagnostic_dump_holding_live")
        ho_info = await _safe_read(_FC_HOLDING, 990, 50, "diagnostic_dump_holding_info")

        if all(b is None for b in (in_live, in_info, ho_live, ho_info)):
            logger.warning("etrel diagnostic dump unavailable — every block read failed")
            return

        lines = [
            f"etrel register dump (address: raw_uint16 | float32_big | float32_little) "
            f"host={self.config.host} unit_id={self.config.unit_id}",
        ]
        self._append_block(lines, "input", "live (regs 0..47)", in_live, base_address=0)
        self._append_block(
            lines, "input", "info (regs 990..1039)", in_info, base_address=990, with_ascii=True
        )
        self._append_block(lines, "holding", "live (regs 0..47)", ho_live, base_address=0)
        self._append_block(
            lines, "holding", "info (regs 990..1039)", ho_info, base_address=990, with_ascii=True
        )
        # Single multi-line log entry — keeps the dump grouped in the log
        # viewer instead of interleaved with subsequent ticks.
        logger.info("\n".join(lines))

    def _append_block(
        self,
        lines: list[str],
        fc: str,
        label: str,
        regs: list[int] | None,
        *,
        base_address: int,
        with_ascii: bool = False,
    ) -> None:
        header = f"  -- {fc} {label} --"
        if regs is None:
            lines.append(f"{header} READ FAILED")
            return
        lines.append(header)
        for i, raw in enumerate(regs):
            addr = base_address + i
            entry = f"  reg {addr:4d}: 0x{raw:04x} ({raw:5d})"
            if i + 1 < len(regs):
                f_big = self._decode_pair(regs, i, word_order="big")
                f_lil = self._decode_pair(regs, i, word_order="little")
                entry += f" | f32_big={f_big:.4g} | f32_lil={f_lil:.4g}"
            lines.append(entry)
        if with_ascii:
            lines.append(f"  -- {fc} {label} as ASCII: {self._registers_to_ascii(regs)!r}")

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
        async with self._comm_lock:
            return await self._read_data_unlocked()

    async def _read_data_unlocked(self) -> DeviceReading | None:
        # Cheapest read first — failure here means the device isn't talking
        # at all (bad unit ID, Modbus disabled, …) and there's no point
        # spending more round-trips on the rest of the fields.
        status_code = await self._read_input_uint16(_IN_STATUS, label="status")

        # One-shot diagnostic snapshot of input + holding spaces on the
        # first successful tick. Lets us identify a shifted register layout
        # OR a function-code mismatch by eyeballing the dump — happens
        # once, not per tick.
        await self._diagnostic_dump()

        # Voltage probe doubles as endianness check. Read the registers once,
        # decode in both orders locally, pick the plausible one (and make it
        # sticky for the session). Critically, we do NOT flip per tick — a
        # stuck-implausible voltage just gets logged once on the dump above
        # and we move on with whatever decode we picked.
        v_regs = await self._read(_FC_INPUT, _IN_VOLTAGE_L1, 2, label="voltage_l1_v")
        v_big = self._decode_pair(v_regs, 0, word_order="big")
        v_lil = self._decode_pair(v_regs, 0, word_order="little")
        if self._looks_like_voltage(v_big):
            self._word_order = "big"
            voltage_l1_v = v_big
        elif self._looks_like_voltage(v_lil):
            self._word_order = "little"
            voltage_l1_v = v_lil
        else:
            voltage_l1_v = v_big if self._word_order == "big" else v_lil

        # Active applied current (input register 4) — what's actually being
        # delivered, regardless of source. Holding register 8 is the
        # write-side setpoint readback (what we last wrote, or what Sonnen
        # has set). Both are useful: setpoint_a is the live truth for the
        # dashboard tile, set_current_a tells us what the override layer
        # has commanded.
        setpoint_a = await self._read_input_float32(_IN_TARGET_CURRENT_A, label="setpoint_a")
        set_current_a: float | None
        try:
            set_current_a = await self._read_holding_float32(
                _HOLD_SET_CURRENT_A, label="set_current_a"
            )
        except DeviceError:
            # Holding-register reads can be selectively blocked even when
            # input reads work; don't kill the tick if they fail.
            set_current_a = None
        current_l1_a = await self._read_input_float32(_IN_CURRENT_L1, label="current_l1_a")
        power_kw = await self._read_input_float32(_IN_POWER_TOTAL_KW, label="power_kw")
        power_w = power_kw * 1000.0

        # Cache the installer ceiling on the first successful tick — re-read
        # only if it never came through, so a transient gap on the static
        # register doesn't permanently mask it.
        if self._custom_max_a is None:
            try:
                self._custom_max_a = await self._read_input_float32(
                    _IN_CUSTOM_MAX_CURRENT_A, label="custom_max_a"
                )
                logger.info("etrel custom max current cached", custom_max_a=self._custom_max_a)
            except DeviceError:
                # Already logged by _read; leave cache empty for next tick.
                pass

        # Per-tick scan for the Sonnen cluster register. Costs one extra
        # Modbus round-trip; gives us the address Sonnen is writing to as
        # soon as the clamp asserts. Targets ``setpoint_a`` (the active
        # applied limit) — whatever holding-register pair currently holds
        # that value is the most-restrictive constraint source.
        cluster_candidates = await self._scan_holding_for_target(setpoint_a)

        self._log_state_transitions(
            status_code=status_code,
            setpoint_a=setpoint_a,
            set_current_a=set_current_a,
            current_l1_a=current_l1_a,
            power_kw=power_kw,
            cluster_candidates=cluster_candidates,
        )

        return DeviceReading(
            device_id=self.device_id,
            data={
                "status_code": float(status_code),
                "status": _status_label(status_code),
                "setpoint_a": setpoint_a,
                "set_current_a": set_current_a,
                "voltage_l1_v": voltage_l1_v,
                "current_l1_a": current_l1_a,
                "power_kw": power_kw,
                "power_w": power_w,
                "custom_max_a": self._custom_max_a,
            },
            quality=1.0,
        )

    @staticmethod
    def _float_changed(prev: float | None, curr: float | None) -> bool:
        """True when ``curr`` differs from ``prev`` by more than the noise floor.

        ``None`` on either side counts as a change unless both are ``None`` —
        catches first-ever-read and went-missing-this-tick transitions.
        """
        if prev is None and curr is None:
            return False
        if prev is None or curr is None:
            return True
        return abs(prev - curr) > _SETPOINT_NOISE_A

    def _log_state_transitions(
        self,
        *,
        status_code: int,
        setpoint_a: float,
        set_current_a: float | None,
        current_l1_a: float,
        power_kw: float,
        cluster_candidates: list[tuple[int, float]],
    ) -> None:
        """Emit one structured log event when the Etrel's observable state moves.

        Designed to make Sonnen Smart-E-Grid behavior legible in the log:
        Sonnen writes the cluster/load-guard channel on port 503 (which we
        do not read), the orchestrator writes the setpoint on holding reg 8
        via port 502, and the EV publishes its own cap. Etrel applies the
        most-restrictive of all three into ``setpoint_a`` (input reg 4).

        Therefore:
        * ``set_current_a`` (holding reg 8 readback) reflects only what *we*
          last wrote.
        * ``setpoint_a`` (input reg 4) reflects whichever source is
          currently the binding constraint.

        ``setpoint_a`` < ``set_current_a`` (beyond noise) means *something
        else* (Sonnen via port 503 / EV cap) is clamping the charger below
        what we asked for — emitted as a one-shot ``setpoint_diverged=True``
        event so a manual-write test can be correlated with the Sonnen
        backend's reaction. The dual readback at every tick is logged at
        DEBUG so verbose tracing is available without ad-hoc instrumentation.

        Per-tick noise stays low: this only emits when something moved
        (status / setpoint_a / set_current_a / divergence flag) past the
        configured noise floor, so a quiet charger produces zero log lines
        while a Sonnen-driven session produces one line per state change.
        """
        first_observation = self._prev_status_code is None

        diverged = (
            set_current_a is not None
            and abs(setpoint_a - set_current_a) > _SETPOINT_NOISE_A
            and setpoint_a < set_current_a
        )

        status_changed = self._prev_status_code != status_code
        setpoint_changed = self._float_changed(self._prev_setpoint_a, setpoint_a)
        set_current_changed = self._float_changed(self._prev_set_current_a, set_current_a)
        diverged_changed = self._prev_diverged != diverged

        # Render the candidate list as ``[(addr, val), …]`` for log
        # readability — addresses are what we ultimately want to lock onto.
        cluster_repr = (
            [{"reg": a, "value": v} for a, v in cluster_candidates]
            if cluster_candidates
            else None
        )

        # DEBUG snapshot every tick — opt-in for users tracing minute-by-minute
        # Sonnen behavior without changing log levels per-source.
        logger.debug(
            "etrel tick snapshot",
            status=_status_label(status_code),
            setpoint_a=setpoint_a,
            set_current_a=set_current_a,
            current_l1_a=current_l1_a,
            power_kw=power_kw,
            setpoint_diverged=diverged,
            setpoint_minus_write=(
                setpoint_a - set_current_a if set_current_a is not None else None
            ),
            cluster_candidates=cluster_repr,
        )

        if (
            first_observation
            or status_changed
            or setpoint_changed
            or set_current_changed
            or diverged_changed
        ):
            event = "etrel state observed (first read)" if first_observation else "etrel state changed"
            logger.info(
                event,
                status_prev=(
                    None if self._prev_status_code is None
                    else _status_label(self._prev_status_code)
                ),
                status=_status_label(status_code),
                setpoint_a_prev=self._prev_setpoint_a,
                setpoint_a=setpoint_a,
                set_current_a_prev=self._prev_set_current_a,
                set_current_a=set_current_a,
                setpoint_diverged_prev=self._prev_diverged,
                setpoint_diverged=diverged,
                # Helps eyeball which actor is winning: 0 → us, negative →
                # someone else clamping below our write, positive → shouldn't
                # happen (Etrel takes the min).
                setpoint_minus_write=(
                    setpoint_a - set_current_a if set_current_a is not None else None
                ),
                custom_max_a=self._custom_max_a,
                # Holding-register addresses whose float32 value matches
                # ``setpoint_a`` — the one Sonnen wrote its cluster limit
                # to is in here. Watch which address persistently tracks
                # the clamp; that's the cluster-max register on this unit.
                cluster_candidates=cluster_repr,
            )

        self._prev_status_code = status_code
        self._prev_setpoint_a = setpoint_a
        self._prev_set_current_a = set_current_a
        self._prev_diverged = diverged

    async def health_check(self) -> bool:
        async with self._comm_lock:
            try:
                await self._read_input_uint16(_IN_STATUS, label="health_check")
            except DeviceError:
                return False
            return True

    async def set_charging_current_a(self, amps: float) -> None:
        async with self._comm_lock:
            await self._set_charging_current_a_unlocked(amps)

    async def _set_charging_current_a_unlocked(self, amps: float) -> None:
        """Write the set-current setpoint (holding regs 8..9, float32).

        Uses **FC 0x06** (``write_register``) twice — once per word — rather
        than FC 0x10 (``write_registers``). The first attempt with FC 0x10
        observed a silent drop on this unit (TCP connect succeeded, the
        Modbus request was sent, but no reply ever came back) which is the
        firmware's documented behavior when the Sonnen Smart-E-Grid
        cluster channel on port 503 holds control authority. FC 0x06 is
        the more universally-accepted Modbus write across firmware
        variants and is what we now use.

        Trade-off: there's a brief (~tens of ms) window between the two
        writes where reg 8 carries the new high word and reg 9 still has
        the old low word, so a concurrent reader could decode a transient
        garbage float. Acceptable for this diagnostic write path; would
        need atomicity guarantees if this were ever moved into a control
        loop.

        Float32 word order matches what the read path detected from L1
        voltage; on a fresh client (no successful read yet) it falls back
        to the documented big-endian default.

        Etrel applies the lower of (this setpoint, installer ceiling, EV
        cap). ``0`` pauses the session; values above ``custom_max_a`` are
        clamped by the charger itself.
        """
        client = await self._ensure_connected()
        regs = AsyncModbusTcpClient.convert_to_registers(
            float(amps),
            AsyncModbusTcpClient.DATATYPE.FLOAT32,
            word_order=self._word_order,
        )
        for word_offset, value in enumerate(regs):
            address = _HOLD_SET_CURRENT_A + word_offset
            ctx = self._log_ctx(_FC_HOLDING, address, 1)
            try:
                result = await client.write_register(
                    address=address,
                    value=value,
                    device_id=self.config.unit_id,
                )
            except TimeoutError as e:
                await self._drop_connection()
                logger.warning(
                    "etrel write timeout (FC 0x06 split)",
                    field="set_current_a",
                    word_offset=word_offset,
                    raw_value=value,
                    **ctx,
                )
                raise DeviceTimeoutError(
                    f"Etrel write timed out at {self._endpoint} "
                    f"(set_current_a word {word_offset + 1}/{len(regs)} "
                    f"amps={amps:.2f})"
                ) from e
            except ModbusException as e:
                await self._drop_connection()
                logger.warning(
                    "etrel write modbus error (FC 0x06 split)",
                    field="set_current_a",
                    word_offset=word_offset,
                    raw_value=value,
                    error=str(e),
                    **ctx,
                )
                raise DeviceConnectionError(
                    f"Etrel write error at {self._endpoint} "
                    f"(set_current_a word {word_offset + 1}/{len(regs)} "
                    f"amps={amps:.2f}): {e}"
                ) from e
            if result.isError():
                logger.warning(
                    "etrel write returned error result (FC 0x06 split)",
                    field="set_current_a",
                    word_offset=word_offset,
                    raw_value=value,
                    result=str(result),
                    **ctx,
                )
                raise DeviceProtocolError(
                    f"Etrel write returned error at {self._endpoint} "
                    f"(set_current_a word {word_offset + 1}/{len(regs)} "
                    f"amps={amps:.2f}): {result}"
                )
        logger.info(
            "etrel set_current_a written (FC 0x06 split)",
            amps=amps,
            words_written=len(regs),
            word_order=self._word_order,
        )

    async def _scan_holding_for_target(
        self, target_a: float
    ) -> list[tuple[int, float]]:
        """Find holding-register pairs whose float32 decode is near ``target_a``.

        Used to identify which register Sonnen's port-503 cluster channel
        writes its current limit to. Etrel applies the most-restrictive of
        all setpoint sources into ``setpoint_a`` (input reg 4) — so when
        ``setpoint_a`` is being clamped below our write, *some* holding
        register pair must hold a float32 equal to that clamp value. This
        scan surfaces it.

        Returns ``(address, value)`` per match, where ``address`` is the
        first register of the pair. ``set_current_a`` (reg 8) is skipped
        — it always reads back our own write, which would otherwise be a
        false positive whenever our write equals the active limit.

        On a read failure returns ``[]`` rather than raising; this is a
        diagnostic, not part of the critical tick path.
        """
        try:
            regs = await self._read(
                _FC_HOLDING, 0, _CLUSTER_SCAN_SPAN, label="cluster_scan"
            )
        except DeviceError:
            return []
        candidates: list[tuple[int, float]] = []
        for i in range(len(regs) - 1):
            if i == _HOLD_SET_CURRENT_A:
                continue
            try:
                v = self._decode_pair(regs, i, word_order=self._word_order)
            except (ValueError, OverflowError):
                continue
            if abs(v - target_a) <= _CLUSTER_MATCH_TOLERANCE_A:
                candidates.append((i, v))
        return candidates

    async def force_diagnostic_dump(self) -> None:
        """Re-run the input+holding register dump (regs 0..47 and 990..1039).

        Used to find which register Sonnen's port-503 cluster channel writes
        to — the startup dump only captures values present at boot, but the
        Sonnen-imposed limit is a runtime value. Trigger this with the
        clamp active, then grep the log for the float32 column matching
        the observed ``setpoint_a``: that register pair is the source.

        Acquires the comm lock so the multi-block read can't interleave
        with the orchestrator's tick.
        """
        async with self._comm_lock:
            self._dump_done = False
            await self._diagnostic_dump()

    async def read_set_current_a(self) -> float:
        """Read the write-side setpoint readback (holding reg 8..9, float32).

        Diagnostic helper used by the API write endpoint to verify whether
        a write took effect, even when the write request itself didn't
        get an ACK. ``set_current_a`` reflects only what *we* (port 502)
        last wrote; if it changed to our requested value, the write
        succeeded silently.

        Raises ``DeviceError`` on failure — caller decides how to surface.
        """
        async with self._comm_lock:
            return await self._read_holding_float32(
                _HOLD_SET_CURRENT_A, label="set_current_a_readback"
            )
