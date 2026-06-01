"""In-memory cache for the latest ``VehicleRecord``.

Single-writer (the tick loop) / multi-reader (web API), mirroring
``SolarCache``. The refresh cadence is driven by config (``tronity
.poll_interval_s``) rather than hard-coded here, because each poll wakes the
car / drains its 12 V battery — the cadence is a deliberate operator knob, so
``is_stale`` / ``replace`` take the max-age as a parameter.

A fetch failure gets a fixed backoff via :meth:`mark_failed` so a flaky cloud
link or a sleeping car can't make the tick loop hammer Tronity every poll.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from energy_orchestrator.vehicle.base import VehicleRecord

# After a failed fetch, wait at least this long before retrying — a sleeping
# car or a transient cloud error shouldn't trigger a poll every tick.
_FAILURE_BACKOFF = timedelta(minutes=5)


class VehicleCache:
    def __init__(self) -> None:
        self._record: VehicleRecord | None = None
        self._last_refresh: datetime | None = None
        # Earliest time the next fetch is allowed. Bumped forward after every
        # attempt: success uses the configured cadence, failure uses the
        # fixed backoff. ``None`` means "no cooldown — fetch when due".
        self._next_attempt_at: datetime | None = None

    def record(self) -> VehicleRecord | None:
        return self._record

    @property
    def last_refresh(self) -> datetime | None:
        return self._last_refresh

    def is_stale(self, now: datetime, max_age: timedelta) -> bool:
        # Honour the per-attempt cooldown first — this is what stops the tick
        # loop re-firing a fetch every poll after a failure.
        if self._next_attempt_at is not None and now < self._next_attempt_at:
            return False
        if self._record is None or self._last_refresh is None:
            return True
        return now - self._last_refresh >= max_age

    def replace(self, record: VehicleRecord, now: datetime, max_age: timedelta) -> None:
        self._record = record
        self._last_refresh = now
        self._next_attempt_at = now + max_age

    def mark_failed(self, now: datetime) -> None:
        """Record a fetch failure so the next retry is delayed.

        Keeps any existing record — a stale-but-readable SoC is more useful to
        the dashboard than nothing while we back off.
        """
        self._next_attempt_at = now + _FAILURE_BACKOFF

    def invalidate(self) -> None:
        """Force a refetch on the next tick (clears the age + cooldown gates)."""
        self._last_refresh = None
        self._next_attempt_at = None
