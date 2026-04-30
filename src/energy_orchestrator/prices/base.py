"""Price-provider abstractions, errors, and the ``PricePoint`` dataclass."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Self

from energy_orchestrator.config.models import PricesConfig


class PriceError(Exception):
    """Base for all price-provider errors."""


class PriceConfigurationError(PriceError):
    """Provider misconfigured (missing key, missing csv_path, unsupported area)."""


class PriceFetchError(PriceError):
    """Network / IO failure: connection refused, HTTP error, file not found."""


class PriceParseError(PriceError):
    """Response or file could not be parsed (bad XML, missing CSV columns)."""


@dataclass(frozen=True)
class PricePoint:
    """One hour of consumption and injection prices.

    ``timestamp`` is the start of the hour, in UTC. Both prices are in EUR/kWh
    and may be negative (injection price often goes negative when the grid is
    oversupplied — this is the trigger for SolarEdge curtailment).
    """

    timestamp: datetime
    consumption_eur_per_kwh: float
    injection_eur_per_kwh: float

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("PricePoint.timestamp must be timezone-aware (UTC)")


class PriceProvider(ABC):
    """Source of day-ahead prices. Concrete providers wrap one external source."""

    def __init__(self, config: PricesConfig) -> None:
        self.config = config

    @abstractmethod
    async def fetch_prices(self, start: datetime, end: datetime) -> Sequence[PricePoint]:
        """Return all hourly price points in ``[start, end)`` (UTC)."""

    async def close(self) -> None:  # noqa: B027 -- intentional no-op default; subclasses override
        """Release any held resources. Default no-op; override when needed."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
