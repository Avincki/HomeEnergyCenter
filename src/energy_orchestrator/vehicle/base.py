"""Vehicle-telemetry abstractions, errors, and dataclasses.

Mirrors the shape of ``solar.base`` / ``prices.base``: a frozen
``VehicleRecord`` (one snapshot of a car's state), an error hierarchy, and an
abstract ``VehicleProvider``.

A vehicle provider reads a *cloud* API (the car's own telemetry) rather than a
local device, so it lives in its own package polled on a slow cadence — not
under ``devices`` (those are local, fast, host/port clients read every tick).
The telemetry is vehicle-centric: it follows a specific VIN wherever the car
physically is, which is why :meth:`VehicleRecord.at_home` exists — a consumer
must confirm the car is actually at the charger before trusting its SoC.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from types import TracebackType
from typing import Self

_EARTH_RADIUS_M = 6_371_000.0


class VehicleError(Exception):
    """Base for all vehicle-provider errors."""


class VehicleConfigurationError(VehicleError):
    """Provider misconfigured (VIN not found, account exposes no vehicle, ...)."""


class VehicleAuthError(VehicleError):
    """Credentials rejected (bad client id/secret, token refused)."""


class VehicleFetchError(VehicleError):
    """Network / IO failure or HTTP non-2xx response."""


class VehicleParseError(VehicleError):
    """Response could not be parsed (bad JSON, missing fields)."""


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS-84 points.

    Good to well under a metre at household geofence scales — far tighter than
    the GPS jitter the geofence radius already has to absorb.
    """
    rlat1, rlat2 = radians(lat1), radians(lat2)
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlon / 2) ** 2
    return 2.0 * _EARTH_RADIUS_M * asin(sqrt(a))


@dataclass(frozen=True)
class VehicleRecord:
    """One snapshot of an EV's state, normalized across providers.

    Every telemetry field is optional — providers return partial records and a
    laggy upstream may omit anything. ``recorded_at`` is when the *car* last
    reported (UTC, may lag wall-clock badly); ``fetched_at`` is when we pulled
    it. The gap between them is the staleness that consumers must guard against.
    """

    fetched_at: datetime
    soc_pct: float | None = None
    plugged: bool | None = None
    charging: str | None = None
    range_km: float | None = None
    odometer_km: float | None = None
    charger_power_kw: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    recorded_at: datetime | None = None

    def age(self, now: datetime) -> timedelta | None:
        """How old the car's own report is, or ``None`` if it carried no timestamp."""
        if self.recorded_at is None:
            return None
        return now - self.recorded_at

    def is_fresh(self, now: datetime, stale_after: timedelta) -> bool:
        """True when the car's report is recent enough to trust.

        A record with no ``recorded_at`` is treated as NOT fresh — we can't
        prove it's recent, and the whole point of the freshness gate is to fail
        closed on unknown age.
        """
        age = self.age(now)
        return age is not None and age <= stale_after

    def at_home(
        self, home_lat: float | None, home_lon: float | None, radius_m: float
    ) -> bool | None:
        """True/False if the car is within ``radius_m`` of home, else ``None``.

        Returns ``None`` (unknown) when either the home geofence isn't
        configured or the record carries no coordinates — callers must treat
        unknown as "can't confirm", never as "yes".
        """
        if home_lat is None or home_lon is None:
            return None
        if self.latitude is None or self.longitude is None:
            return None
        return haversine_m(self.latitude, self.longitude, home_lat, home_lon) <= radius_m

    def at_home_confirmed(
        self,
        now: datetime,
        stale_after: timedelta,
        home_lat: float | None,
        home_lon: float | None,
        radius_m: float,
    ) -> bool:
        """True only when "at home" is corroborated by a live channel.

        The geofence verdict alone is not trustworthy: the upstream position
        (and odometer) channel can freeze on the last home fix while SoC keeps
        flowing, so the record stays ``fresh`` around a stale location
        (observed 2026-07-15: car 80 km away, fix pinned 2 m from home).
        ``plugged`` is delivered live — it flipped at departure — so a plugged
        car whose fix is inside the geofence really is on the home charger.

        Fail-closed: ``False`` means "can't confirm", never "away" — callers
        may decline to assert "at home" but must not block on it.
        """
        return (
            self.plugged is True
            and self.is_fresh(now, stale_after)
            and self.at_home(home_lat, home_lon, radius_m) is True
        )


class VehicleProvider(ABC):
    """Source of EV telemetry. One concrete provider per upstream cloud API."""

    @abstractmethod
    async def fetch_record(self) -> VehicleRecord:
        """Fetch the latest telemetry snapshot for the configured vehicle."""

    async def close(self) -> None:  # noqa: B027 -- intentional no-op default; subclasses override
        """Release any held resources (HTTP session). Default no-op."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
