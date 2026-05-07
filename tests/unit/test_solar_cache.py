from __future__ import annotations

from datetime import UTC, datetime, timedelta

from energy_orchestrator.solar.base import SolarForecast, SolarPoint
from energy_orchestrator.solar.cache import SolarCache


def _forecast() -> SolarForecast:
    return SolarForecast(
        points=(SolarPoint(timestamp=datetime(2026, 5, 7, 10, 0, tzinfo=UTC), watts=1500.0),),
        per_plane={},
        watt_hours_today=8000.0,
        watt_hours_tomorrow=9000.0,
    )


def test_empty_cache_is_stale() -> None:
    cache = SolarCache()
    assert cache.is_stale(datetime(2026, 5, 7, 12, 0, tzinfo=UTC)) is True


def test_fresh_cache_is_not_stale_for_max_age() -> None:
    cache = SolarCache()
    t0 = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    cache.replace(_forecast(), t0)
    assert cache.is_stale(t0) is False
    assert cache.is_stale(t0 + timedelta(minutes=29)) is False
    assert cache.is_stale(t0 + timedelta(minutes=30)) is True


def test_failed_fetch_triggers_60min_backoff() -> None:
    """After a fetch failure (e.g. 429), is_stale stays False for an hour
    even though no successful forecast is in the cache. Without this the
    tick loop would keep firing fetches every poll — burning quota and
    spamming the log."""
    cache = SolarCache()
    t0 = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    cache.mark_failed(t0)
    assert cache.is_stale(t0) is False
    assert cache.is_stale(t0 + timedelta(minutes=59)) is False
    assert cache.is_stale(t0 + timedelta(minutes=60)) is True


def test_failure_backoff_does_not_clear_existing_forecast() -> None:
    """A stale-but-readable forecast remains visible during backoff so the
    dashboard doesn't go blank because of one transient API failure."""
    cache = SolarCache()
    t0 = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    cache.replace(_forecast(), t0)
    cache.mark_failed(t0 + timedelta(minutes=45))
    assert cache.forecast() is not None
    assert cache.forecast().watt_hours_today == 8000.0


def test_success_overrides_failure_backoff() -> None:
    cache = SolarCache()
    t0 = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    cache.mark_failed(t0)
    # A subsequent success should reset the cooldown to the normal 30-min
    # cadence, not retain the 60-min failure window.
    cache.replace(_forecast(), t0 + timedelta(minutes=10))
    assert cache.is_stale(t0 + timedelta(minutes=10)) is False
    assert cache.is_stale(t0 + timedelta(minutes=39)) is False
    assert cache.is_stale(t0 + timedelta(minutes=40)) is True
