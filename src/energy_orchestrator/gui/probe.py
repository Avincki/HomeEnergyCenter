"""Async-to-tk bridge for the GUI's connection-test buttons.

Each "Test" button asks one device for ``health_check()``. We don't want
to block the tk main loop, so the call runs on a private event loop in a
worker thread and posts the boolean result + any error message back via
a callback that tk will invoke from the main thread.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass

from energy_orchestrator.config.models import DeviceConfig
from energy_orchestrator.devices import (
    DeviceClient,
    DeviceError,
    create_device_client,
)


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one connection probe."""

    ok: bool
    message: str  # human-readable summary, empty on success


def probe_device(
    config: DeviceConfig,
    callback: Callable[[ProbeResult], None],
) -> threading.Thread:
    """Run ``health_check`` on a fresh client built from ``config``.

    The probe runs on a daemon thread with its own event loop. ``callback``
    is invoked exactly once when the probe finishes (success, failure, or
    unexpected exception). Returns the thread so callers can ``join`` in
    tests; production GUI code can ignore the return value.
    """

    def _worker() -> None:
        try:
            result = asyncio.run(_run(config))
        except Exception as e:  # defensive — never let probe kill the GUI
            result = ProbeResult(ok=False, message=f"unexpected: {e}")
        callback(result)

    thread = threading.Thread(target=_worker, daemon=True, name="eo-gui-probe")
    thread.start()
    return thread


async def _run(config: DeviceConfig) -> ProbeResult:
    client: DeviceClient[DeviceConfig] = create_device_client(config)
    try:
        try:
            ok = await client.health_check()
        except DeviceError as e:
            return ProbeResult(ok=False, message=str(e))
        if ok:
            return ProbeResult(ok=True, message="reachable")
        return ProbeResult(ok=False, message="health check returned False")
    finally:
        await client.close()
