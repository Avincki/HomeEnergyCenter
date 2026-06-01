from __future__ import annotations

from datetime import UTC, datetime, timedelta

from energy_orchestrator.vehicle import VehicleCache, VehicleRecord

_MAX_AGE = timedelta(seconds=900)


def _record(now: datetime) -> VehicleRecord:
    return VehicleRecord(fetched_at=now, soc_pct=72.0)


def test_empty_cache_is_stale() -> None:
    cache = VehicleCache()
    assert cache.record() is None
    assert cache.is_stale(datetime(2026, 6, 1, tzinfo=UTC), _MAX_AGE) is True


def test_fresh_after_replace_then_stale_past_max_age() -> None:
    cache = VehicleCache()
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    cache.replace(_record(now), now, _MAX_AGE)
    assert cache.record() is not None
    # Just before max age -> still fresh.
    assert cache.is_stale(now + timedelta(seconds=899), _MAX_AGE) is False
    # At/after max age -> stale again.
    assert cache.is_stale(now + timedelta(seconds=900), _MAX_AGE) is True


def test_failure_backoff_suppresses_immediate_retry() -> None:
    cache = VehicleCache()
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    cache.mark_failed(now)
    # Within the backoff window the cache reports "not stale" so the tick loop
    # won't re-fetch every poll.
    assert cache.is_stale(now + timedelta(minutes=1), _MAX_AGE) is False
    # After the backoff it's fetchable again (still empty -> stale).
    assert cache.is_stale(now + timedelta(minutes=6), _MAX_AGE) is True


def test_mark_failed_keeps_existing_record() -> None:
    cache = VehicleCache()
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    cache.replace(_record(now), now, _MAX_AGE)
    cache.mark_failed(now + timedelta(seconds=950))
    # A stale-but-readable SoC is still served to the dashboard.
    assert cache.record() is not None
    assert cache.record().soc_pct == 72.0  # type: ignore[union-attr]


def test_invalidate_forces_refetch() -> None:
    cache = VehicleCache()
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    cache.replace(_record(now), now, _MAX_AGE)
    assert cache.is_stale(now, _MAX_AGE) is False
    cache.invalidate()
    assert cache.is_stale(now, _MAX_AGE) is True
