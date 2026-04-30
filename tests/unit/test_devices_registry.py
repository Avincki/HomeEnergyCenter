from __future__ import annotations

from collections.abc import Iterator

import pytest

from energy_orchestrator.config import (
    CarChargerConfig,
    SolarEdgeConfig,
    SonnenApiVersion,
    SonnenBatterieConfig,
)
from energy_orchestrator.data.models import SourceName
from energy_orchestrator.devices import (
    DeviceClient,
    DeviceReading,
    UnknownDeviceTypeError,
    create_device_client,
    register_device,
    registered_configs,
)
from energy_orchestrator.devices.registry import (
    _clear_registry_for_tests,
    _restore_registry_for_tests,
    _snapshot_registry_for_tests,
    _unregister_for_tests,
)


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Snapshot the registry, run with a clean slate, then restore.

    Concrete clients (sonnen, etc.) register themselves at import time;
    other tests rely on those registrations, so we must not lose them.
    """
    snapshot = _snapshot_registry_for_tests()
    _clear_registry_for_tests()
    try:
        yield
    finally:
        _restore_registry_for_tests(snapshot)


class _StubSonnenClient(DeviceClient[SonnenBatterieConfig]):
    source_name = SourceName.SONNEN

    async def read_data(self) -> DeviceReading | None:
        return DeviceReading(device_id=self.device_id, data={"USOC": 50})

    async def health_check(self) -> bool:
        return True


def _sonnen_cfg() -> SonnenBatterieConfig:
    return SonnenBatterieConfig(
        host="1.1.1.1",
        api_version=SonnenApiVersion.V2,
        auth_token="t",
        capacity_kwh=10.0,
    )


def test_register_and_create() -> None:
    register_device(SonnenBatterieConfig)(_StubSonnenClient)
    client = create_device_client(_sonnen_cfg())
    assert isinstance(client, _StubSonnenClient)
    assert client.config.capacity_kwh == 10.0


def test_create_unregistered_raises() -> None:
    with pytest.raises(UnknownDeviceTypeError, match="SonnenBatterieConfig"):
        create_device_client(_sonnen_cfg())


def test_register_multiple_configs_one_client() -> None:
    register_device(SonnenBatterieConfig, SolarEdgeConfig)(_StubSonnenClient)
    assert SonnenBatterieConfig in registered_configs()
    assert SolarEdgeConfig in registered_configs()


def test_double_register_same_class_is_idempotent() -> None:
    register_device(SonnenBatterieConfig)(_StubSonnenClient)
    register_device(SonnenBatterieConfig)(_StubSonnenClient)  # same class — fine


def test_double_register_different_class_raises() -> None:
    class _Other(DeviceClient[SonnenBatterieConfig]):
        source_name = SourceName.SONNEN

        async def read_data(self) -> DeviceReading | None:
            return None

        async def health_check(self) -> bool:
            return False

    register_device(SonnenBatterieConfig)(_StubSonnenClient)
    with pytest.raises(RuntimeError, match="already registered"):
        register_device(SonnenBatterieConfig)(_Other)


def test_lookup_is_exact_type_not_isa() -> None:
    """Subclasses of a registered config don't inherit registration."""

    class _ExtendedSonnen(SonnenBatterieConfig):
        pass

    register_device(SonnenBatterieConfig)(_StubSonnenClient)
    extended = _ExtendedSonnen(
        host="1.1.1.1",
        api_version=SonnenApiVersion.V2,
        auth_token="t",
        capacity_kwh=10.0,
    )
    with pytest.raises(UnknownDeviceTypeError):
        create_device_client(extended)


def test_unregister_helper() -> None:
    register_device(SonnenBatterieConfig)(_StubSonnenClient)
    _unregister_for_tests(SonnenBatterieConfig)
    with pytest.raises(UnknownDeviceTypeError):
        create_device_client(_sonnen_cfg())


def test_device_id_defaults_to_source_name() -> None:
    register_device(SonnenBatterieConfig)(_StubSonnenClient)
    client = create_device_client(_sonnen_cfg())
    assert client.device_id == "sonnen"


def test_unrelated_config_unregistered() -> None:
    register_device(SonnenBatterieConfig)(_StubSonnenClient)
    cfg = CarChargerConfig(host="1.1.1.2")
    with pytest.raises(UnknownDeviceTypeError, match="CarChargerConfig"):
        create_device_client(cfg)
