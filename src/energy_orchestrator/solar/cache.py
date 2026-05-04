"""In-memory cache for the latest ``SolarForecast``.

Single-writer (the tick loop) / multi-reader (web API). Refresh policy:
Forecast.Solar's weather model updates ~hourly and they explicitly ask
clients not to poll more often than every 15 min, so we refresh at most
every 30 min — well below per-IP rate limits even with 4 planes.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from energy_orchestrator.solar.base import SolarForecast

_MAX_AGE = timedelta(minutes=30)


class SolarCache:
    def __init__(self) -> None:
        self._forecast: SolarForecast | None = None
        self._last_refresh: datetime | None = None

    def forecast(self) -> SolarForecast | None:
        return self._forecast

    @property
    def last_refresh(self) -> datetime | None:
        return self._last_refresh

    def is_stale(self, now: datetime) -> bool:
        if self._forecast is None or self._last_refresh is None:
            return True
        return now - self._last_refresh >= _MAX_AGE

    def replace(self, forecast: SolarForecast, now: datetime) -> None:
        self._forecast = forecast
        self._last_refresh = now
