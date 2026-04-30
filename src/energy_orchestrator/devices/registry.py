from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from energy_orchestrator.config.models import DeviceConfig
from energy_orchestrator.devices.base import DeviceClient
from energy_orchestrator.devices.errors import UnknownDeviceTypeError

ClientT = TypeVar("ClientT", bound=DeviceClient[Any])

_REGISTRY: dict[type[DeviceConfig], type[DeviceClient[Any]]] = {}


def register_device(
    *config_types: type[DeviceConfig],
) -> Callable[[type[ClientT]], type[ClientT]]:
    """Decorator that maps one-or-more config classes to a client class.

    Multiple config types may share a client (e.g. all HomeWizard meters
    use the same JSON shape). Registering the same config twice raises.
    """

    def decorator(client_cls: type[ClientT]) -> type[ClientT]:
        for cfg_type in config_types:
            existing = _REGISTRY.get(cfg_type)
            if existing is not None and existing is not client_cls:
                raise RuntimeError(
                    f"{cfg_type.__name__} is already registered to "
                    f"{existing.__name__}; cannot also register {client_cls.__name__}"
                )
            _REGISTRY[cfg_type] = client_cls
        return client_cls

    return decorator


def create_device_client(config: DeviceConfig) -> DeviceClient[Any]:
    """Instantiate the client class registered for ``type(config)``.

    Lookup is exact-match: subclassing a config does not inherit registration
    (subclasses of a registered config must register explicitly, since they
    typically need their own client logic).
    """
    cls = _REGISTRY.get(type(config))
    if cls is None:
        raise UnknownDeviceTypeError(f"no device client registered for {type(config).__name__}")
    return cls(config)


def registered_configs() -> frozenset[type[DeviceConfig]]:
    """Snapshot of currently-registered config types — for tests / introspection."""
    return frozenset(_REGISTRY.keys())


def _clear_registry_for_tests() -> None:
    """Wipe the registry. Test-only — production code must not call this."""
    _REGISTRY.clear()


def _unregister_for_tests(config_type: type[DeviceConfig]) -> None:
    """Remove a single registration. Test-only."""
    _REGISTRY.pop(config_type, None)


def _snapshot_registry_for_tests() -> dict[type[DeviceConfig], type[DeviceClient[Any]]]:
    """Return a copy of the current registry. Test-only — pair with restore."""
    return dict(_REGISTRY)


def _restore_registry_for_tests(
    snapshot: dict[type[DeviceConfig], type[DeviceClient[Any]]],
) -> None:
    """Replace the registry contents with a previously-taken snapshot. Test-only."""
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)
