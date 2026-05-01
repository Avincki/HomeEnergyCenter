"""In-memory price cache populated by the orchestrator tick loop.

The cache holds the most recent successful set of ``PricePoint``s from the
configured provider. Reads are non-async and side-effect-free; refreshes
happen on the tick-loop side via :py:meth:`PriceCache.replace`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from energy_orchestrator.prices.base import PricePoint

# Refresh at least this often even if the cache still nominally covers "now".
# ENTSO-E publishes day-ahead prices once daily but the user may also be
# running a CSV provider whose file got hand-edited; an hourly re-pull
# guarantees those edits land within the hour without complicating the API.
_MAX_AGE = timedelta(hours=1)


class PriceCache:
    """Thread-unsafe single-writer cache. The tick loop is the only writer."""

    def __init__(self) -> None:
        self._points: tuple[PricePoint, ...] = ()
        self._last_refresh: datetime | None = None

    def points(self) -> tuple[PricePoint, ...]:
        return self._points

    @property
    def last_refresh(self) -> datetime | None:
        return self._last_refresh

    def is_stale(self, now: datetime) -> bool:
        if self._last_refresh is None:
            return True
        if now - self._last_refresh >= _MAX_AGE:
            return True
        # If the cache no longer covers ``now``, refresh — covers the case where
        # the orchestrator was paused overnight and yesterday's prices have
        # all expired.
        latest_end = max(
            (p.timestamp + timedelta(hours=1) for p in self._points),
            default=None,
        )
        return latest_end is None or latest_end <= now

    def replace(self, points: Iterable[PricePoint], now: datetime) -> None:
        self._points = tuple(sorted(points, key=lambda p: p.timestamp))
        self._last_refresh = now

    def points_in_range(self, start: datetime, end: datetime) -> Sequence[PricePoint]:
        """Subset of cached points whose timestamp lies in ``[start, end)``."""
        return tuple(p for p in self._points if start <= p.timestamp < end)
