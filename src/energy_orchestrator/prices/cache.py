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
# Backoff between eager attempts to pull *tomorrow's* day-ahead prices once the
# orchestrator decides they're due (past the publication buffer). ENTSO-E can
# publish late on decoupling days, so we keep retrying — but only every few
# minutes, not every poll, to avoid hammering the API while we wait.
_TOMORROW_RETRY = timedelta(minutes=10)


class PriceCache:
    """Thread-unsafe single-writer cache. The tick loop is the only writer."""

    def __init__(self) -> None:
        self._points: tuple[PricePoint, ...] = ()
        self._last_refresh: datetime | None = None
        # Earliest time the eager "fetch tomorrow's prices" path may retry after
        # a fetch that came back without tomorrow's data. ``None`` means no
        # backoff active. Cleared on a successful ``replace`` (whatever it
        # contained) and on ``invalidate``; bumped by ``mark_tomorrow_missing``.
        self._next_day_retry_at: datetime | None = None

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
        # A successful refresh resets the eager-tomorrow backoff: if tomorrow's
        # prices landed the orchestrator stops asking; if they didn't, it
        # re-arms the backoff explicitly via mark_tomorrow_missing.
        self._next_day_retry_at = None

    def invalidate(self) -> None:
        """Force the next ``is_stale`` check to return True (refetch next tick).

        Used when pricing config (injection factor/offset, area) changes at
        runtime so the new values are re-applied at the next tick instead of
        waiting out the normal refresh window.
        """
        self._last_refresh = None
        self._next_day_retry_at = None

    def mark_tomorrow_missing(self, now: datetime) -> None:
        """Record that a fetch did not return tomorrow's prices yet.

        Backs off the eager-tomorrow path so the tick loop retries every
        ``_TOMORROW_RETRY`` rather than every poll while waiting for a late
        day-ahead publication. Does not touch the cached points.
        """
        self._next_day_retry_at = now + _TOMORROW_RETRY

    def tomorrow_retry_allowed(self, now: datetime) -> bool:
        """Whether the eager-tomorrow fetch may run now (backoff elapsed)."""
        return self._next_day_retry_at is None or now >= self._next_day_retry_at

    def points_in_range(self, start: datetime, end: datetime) -> Sequence[PricePoint]:
        """Subset of cached points whose timestamp lies in ``[start, end)``."""
        return tuple(p for p in self._points if start <= p.timestamp < end)
