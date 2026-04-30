from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from energy_orchestrator.devices import DeviceReading


def test_reading_defaults_quality_and_timestamp() -> None:
    r = DeviceReading(device_id="sonnen", data={"USOC": 50})
    assert r.quality == 1.0
    # Default timestamp is roughly now-ish.
    assert (datetime.now(UTC) - r.timestamp).total_seconds() < 5


def test_reading_quality_must_be_in_range() -> None:
    with pytest.raises(ValueError, match="quality"):
        DeviceReading(device_id="x", data={}, quality=1.5)
    with pytest.raises(ValueError, match="quality"):
        DeviceReading(device_id="x", data={}, quality=-0.1)


def test_reading_quality_zero_and_one_allowed() -> None:
    DeviceReading(device_id="x", data={}, quality=0.0)
    DeviceReading(device_id="x", data={}, quality=1.0)


def test_reading_device_id_required() -> None:
    with pytest.raises(ValueError, match="device_id"):
        DeviceReading(device_id="", data={})


def test_reading_is_frozen() -> None:
    r = DeviceReading(device_id="sonnen", data={"USOC": 50})
    with pytest.raises(FrozenInstanceError):
        r.device_id = "other"  # type: ignore[misc]
