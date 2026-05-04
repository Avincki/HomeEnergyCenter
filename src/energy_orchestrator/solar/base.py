"""Solar-forecast abstractions, errors, and dataclasses.

Mirrors the shape of ``prices.base``: a frozen ``SolarPoint`` (one
timestamp + watts), an aggregate ``SolarForecast`` (the per-tick output
shared with the dashboard), and an abstract ``SolarProvider``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Self

from energy_orchestrator.config.models import SolarConfig


class SolarError(Exception):
    """Base for all solar-provider errors."""


class SolarConfigurationError(SolarError):
    """Provider misconfigured (no planes, missing key on a paid tier, ...)."""


class SolarFetchError(SolarError):
    """Network / IO failure or HTTP non-2xx response."""


class SolarParseError(SolarError):
    """Response could not be parsed (bad JSON, missing fields, bad timestamp)."""


@dataclass(frozen=True)
class SolarPoint:
    """One time-bucket of expected production.

    ``timestamp`` is the **start** of the bucket in UTC. ``watts`` is the
    instantaneous expected power across all configured planes.
    """

    timestamp: datetime
    watts: float

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("SolarPoint.timestamp must be timezone-aware (UTC)")


@dataclass(frozen=True)
class SolarForecast:
    """Aggregate forecast as returned by a provider for one fetch.

    ``points`` is the summed-across-planes time series (chronological, UTC).
    ``per_plane`` keeps each plane's series for chart breakdowns.
    ``watt_hours_today`` is the integrated total for the local calendar day
    at the configured location (Forecast.Solar provides this directly).
    """

    points: tuple[SolarPoint, ...]
    per_plane: dict[str, tuple[SolarPoint, ...]] = field(default_factory=dict)
    watt_hours_today: float | None = None
    watt_hours_tomorrow: float | None = None


class SolarProvider(ABC):
    """Source of expected PV production. One concrete provider per upstream."""

    def __init__(self, config: SolarConfig) -> None:
        self.config = config

    @abstractmethod
    async def fetch_forecast(self) -> SolarForecast:
        """Return the latest forecast across all configured planes."""

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


def sum_planes(per_plane: dict[str, Sequence[SolarPoint]]) -> tuple[SolarPoint, ...]:
    """Sum watts across planes that share a timestamp.

    Planes are expected to align on hourly buckets; mismatches still merge
    cleanly (a missing plane at a given hour just contributes 0 W).
    """
    bucket: dict[datetime, float] = {}
    for series in per_plane.values():
        for p in series:
            bucket[p.timestamp] = bucket.get(p.timestamp, 0.0) + p.watts
    return tuple(SolarPoint(timestamp=ts, watts=w) for ts, w in sorted(bucket.items()))
