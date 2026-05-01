from __future__ import annotations

import threading
from typing import Any

import pytest

from energy_orchestrator.config.models import (
    P1MeterConfig,
    SonnenApiVersion,
    SonnenBatterieConfig,
)
from energy_orchestrator.devices import (
    DeviceClient,
    DeviceConnectionError,
    DeviceReading,
)
from energy_orchestrator.gui.probe import ProbeResult, probe_device


def _wait(callback_event: threading.Event, *, timeout: float = 5.0) -> None:
    if not callback_event.wait(timeout):
        raise AssertionError("probe callback never fired")


def _stub_create_device_client(
    monkeypatch: pytest.MonkeyPatch,
    client: DeviceClient[Any],
) -> None:
    monkeypatch.setattr(
        "energy_orchestrator.gui.probe.create_device_client",
        lambda _config: client,
    )


class _FakeClient:
    """Minimal duck-typed stand-in for DeviceClient used by the probe."""

    def __init__(
        self,
        *,
        health_returns: bool = True,
        raise_on_health: BaseException | None = None,
    ) -> None:
        self._returns = health_returns
        self._raise = raise_on_health
        self.closed = False

    async def health_check(self) -> bool:
        if self._raise is not None:
            raise self._raise
        return self._returns

    async def read_data(self) -> DeviceReading | None:
        return None

    async def close(self) -> None:
        self.closed = True


def _sonnen_cfg() -> SonnenBatterieConfig:
    return SonnenBatterieConfig(
        host="192.0.2.10",
        api_version=SonnenApiVersion.V2,
        auth_token="dummy",
        capacity_kwh=10.0,
    )


def test_probe_returns_ok_on_health_check_true(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(health_returns=True)
    _stub_create_device_client(monkeypatch, fake)  # type: ignore[arg-type]

    received: list[ProbeResult] = []
    done = threading.Event()

    def cb(result: ProbeResult) -> None:
        received.append(result)
        done.set()

    thread = probe_device(_sonnen_cfg(), cb)
    thread.join(timeout=5.0)
    _wait(done)

    assert len(received) == 1
    assert received[0].ok is True
    assert received[0].message == "reachable"
    assert fake.closed is True


def test_probe_returns_fail_on_health_check_false(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(health_returns=False)
    _stub_create_device_client(monkeypatch, fake)  # type: ignore[arg-type]

    received: list[ProbeResult] = []
    done = threading.Event()

    def cb(result: ProbeResult) -> None:
        received.append(result)
        done.set()

    thread = probe_device(_sonnen_cfg(), cb)
    thread.join(timeout=5.0)
    _wait(done)

    assert received[0].ok is False
    assert "False" in received[0].message
    assert fake.closed is True


def test_probe_maps_device_error_to_message(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(raise_on_health=DeviceConnectionError("connection refused"))
    _stub_create_device_client(monkeypatch, fake)  # type: ignore[arg-type]

    received: list[ProbeResult] = []
    done = threading.Event()

    def cb(result: ProbeResult) -> None:
        received.append(result)
        done.set()

    thread = probe_device(P1MeterConfig(host="192.0.2.20"), cb)
    thread.join(timeout=5.0)
    _wait(done)

    assert received[0].ok is False
    assert "connection refused" in received[0].message
    assert fake.closed is True


def test_probe_catches_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(raise_on_health=RuntimeError("boom"))
    _stub_create_device_client(monkeypatch, fake)  # type: ignore[arg-type]

    received: list[ProbeResult] = []
    done = threading.Event()

    def cb(result: ProbeResult) -> None:
        received.append(result)
        done.set()

    thread = probe_device(P1MeterConfig(host="192.0.2.20"), cb)
    thread.join(timeout=5.0)
    _wait(done)

    assert received[0].ok is False
    assert "unexpected" in received[0].message
