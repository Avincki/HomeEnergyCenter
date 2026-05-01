from __future__ import annotations

from datetime import UTC, datetime, timedelta

from energy_orchestrator.prices import PriceCache, PricePoint


def _pp(hour: int) -> PricePoint:
    return PricePoint(
        timestamp=datetime(2026, 5, 1, hour, 0, tzinfo=UTC),
        consumption_eur_per_kwh=0.20,
        injection_eur_per_kwh=0.05,
    )


def test_empty_cache_is_stale() -> None:
    cache = PriceCache()
    assert cache.is_stale(datetime(2026, 5, 1, 12, 0, tzinfo=UTC))
    assert cache.points() == ()
    assert cache.last_refresh is None


def test_replace_records_refresh_and_sorts() -> None:
    cache = PriceCache()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    cache.replace([_pp(15), _pp(10), _pp(12)], now)
    hours = [p.timestamp.hour for p in cache.points()]
    assert hours == [10, 12, 15]
    assert cache.last_refresh == now


def test_is_stale_after_an_hour() -> None:
    cache = PriceCache()
    refreshed_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    cache.replace([_pp(13), _pp(14)], refreshed_at)
    assert not cache.is_stale(refreshed_at + timedelta(minutes=30))
    assert cache.is_stale(refreshed_at + timedelta(hours=1))


def test_is_stale_when_cache_does_not_cover_now() -> None:
    cache = PriceCache()
    refreshed_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    cache.replace([_pp(10), _pp(11)], refreshed_at)
    # latest point covers 11:00-12:00; at 12:00 the cache is exhausted.
    assert cache.is_stale(datetime(2026, 5, 1, 12, 0, tzinfo=UTC))


def test_points_in_range_filters_inclusively_on_start_exclusively_on_end() -> None:
    cache = PriceCache()
    now = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    cache.replace([_pp(0), _pp(1), _pp(2), _pp(3)], now)
    subset = cache.points_in_range(
        start=datetime(2026, 5, 1, 1, 0, tzinfo=UTC),
        end=datetime(2026, 5, 1, 3, 0, tzinfo=UTC),
    )
    hours = [p.timestamp.hour for p in subset]
    assert hours == [1, 2]
