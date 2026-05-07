"""In-memory cache for the latest ``SolarForecast``.

Single-writer (the tick loop) / multi-reader (web API). Refresh policy:
Forecast.Solar's weather model updates ~hourly and they explicitly ask
clients not to poll more often than every 15 min, so we refresh at most
every 30 min on success — well below per-IP rate limits even with 4
planes.

Failures get a longer cooldown via :meth:`mark_failed`. Forecast.Solar's
free tier is rate-limited per IP and once we hit 429 we have to wait a
full hour for the bucket to refill; without a failure backoff the tick
loop would otherwise re-fetch every poll and spam the log (and
potentially the API) until quota resets the next hour.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from energy_orchestrator.solar.base import SolarForecast

_MAX_AGE = timedelta(minutes=30)
# After a fetch failure (429, network blip, parse error, …) we wait at
# least this long before retrying. The free Forecast.Solar tier resets
# its quota hourly, so anything shorter just keeps us throttled.
_FAILURE_BACKOFF = timedelta(minutes=60)


class SolarCache:
    def __init__(self) -> None:
        self._forecast: SolarForecast | None = None
        self._last_refresh: datetime | None = None
        # Earliest time the next fetch is allowed. ``None`` means "no
        # cooldown active" — fetch is allowed when the cache is empty or
        # past _MAX_AGE. After every fetch attempt (success or failure)
        # this is bumped forward; success uses _MAX_AGE, failure uses
        # _FAILURE_BACKOFF.
        self._next_attempt_at: datetime | None = None

    def forecast(self) -> SolarForecast | None:
        return self._forecast

    @property
    def last_refresh(self) -> datetime | None:
        return self._last_refresh

    def is_stale(self, now: datetime) -> bool:
        # Honour the per-attempt cooldown first — this is what stops the
        # tick loop from re-firing fetches every poll after a 429.
        if self._next_attempt_at is not None and now < self._next_attempt_at:
            return False
        if self._forecast is None or self._last_refresh is None:
            return True
        return now - self._last_refresh >= _MAX_AGE

    def replace(self, forecast: SolarForecast, now: datetime) -> None:
        self._forecast = forecast
        self._last_refresh = now
        self._next_attempt_at = now + _MAX_AGE

    def mark_failed(self, now: datetime) -> None:
        """Record a fetch failure so the next retry is delayed.

        Does not clear the existing forecast — a stale-but-readable forecast
        is more useful to the dashboard than nothing while we back off.
        """
        self._next_attempt_at = now + _FAILURE_BACKOFF
