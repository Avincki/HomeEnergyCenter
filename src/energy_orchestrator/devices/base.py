from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, ClassVar, Generic, Self, TypeVar

from energy_orchestrator.config.models import DeviceConfig
from energy_orchestrator.data.models import SourceName

ConfigT = TypeVar("ConfigT", bound=DeviceConfig)


@dataclass(frozen=True)
class DeviceReading:
    """One successful read from a device.

    ``data`` is the normalized payload (units and naming as defined by the
    concrete client). ``quality`` is in [0.0, 1.0]: 1.0 = full data, lower
    values indicate partial reads (e.g. some fields missing).
    """

    device_id: str
    data: dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    quality: float = 1.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.quality <= 1.0:
            raise ValueError(f"quality must be in [0.0, 1.0], got {self.quality}")
        if not self.device_id:
            raise ValueError("device_id must not be empty")


class DeviceClient(ABC, Generic[ConfigT]):
    """Abstract base for all device clients.

    Concrete subclasses set ``source_name`` (used for source-status tracking)
    and implement ``read_data`` / ``health_check``. They typically also
    register themselves with ``@register_device(ConfigType)``.
    """

    source_name: ClassVar[SourceName]

    def __init__(self, config: ConfigT) -> None:
        self.config = config

    @property
    def device_id(self) -> str:
        """Stable identifier used in DeviceReading and source-status records.

        Defaults to the source_name; override if a deployment runs multiple
        instances of the same device type.
        """
        return str(self.source_name)

    @abstractmethod
    async def read_data(self) -> DeviceReading | None:
        """Read current data from the device. Return ``None`` if no data is
        available but the failure was non-exceptional (e.g. transient gap).
        Raises ``DeviceError`` on hard failures.
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """Lightweight reachability/auth probe. Should not raise on the
        normal failure modes — return False instead.
        """

    async def close(self) -> None:
        """Release any held resources (HTTP sessions, Modbus connections).

        Default is a no-op; override when needed.
        """

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
