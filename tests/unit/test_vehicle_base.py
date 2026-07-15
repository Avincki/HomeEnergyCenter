from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from energy_orchestrator.vehicle import VehicleRecord, haversine_m

# Gent, BE (matches the example config's solar location).
_HOME_LAT = 51.0543
_HOME_LON = 3.7174


def _record(**kw: object) -> VehicleRecord:
    base: dict[str, object] = {"fetched_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC)}
    base.update(kw)
    return VehicleRecord(**base)  # type: ignore[arg-type]


def test_haversine_zero_for_same_point() -> None:
    assert haversine_m(_HOME_LAT, _HOME_LON, _HOME_LAT, _HOME_LON) == pytest.approx(0.0, abs=1e-6)


def test_haversine_known_short_distance() -> None:
    # ~100 m north (0.0009 deg latitude ≈ 100 m).
    d = haversine_m(_HOME_LAT, _HOME_LON, _HOME_LAT + 0.0009, _HOME_LON)
    assert d == pytest.approx(100.0, abs=2.0)


def test_age_none_without_recorded_at() -> None:
    rec = _record(recorded_at=None)
    assert rec.age(datetime(2026, 6, 1, 12, 0, tzinfo=UTC)) is None


def test_age_and_freshness() -> None:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    rec = _record(recorded_at=now - timedelta(minutes=20))
    assert rec.age(now) == timedelta(minutes=20)
    assert rec.is_fresh(now, timedelta(minutes=30)) is True
    assert rec.is_fresh(now, timedelta(minutes=10)) is False


def test_freshness_false_when_no_timestamp() -> None:
    # Fail closed: unknown age is never "fresh".
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    assert _record(recorded_at=None).is_fresh(now, timedelta(hours=1)) is False


def test_at_home_true_within_radius() -> None:
    rec = _record(latitude=_HOME_LAT + 0.0005, longitude=_HOME_LON)
    assert rec.at_home(_HOME_LAT, _HOME_LON, radius_m=200.0) is True


def test_at_home_false_outside_radius() -> None:
    rec = _record(latitude=_HOME_LAT + 0.05, longitude=_HOME_LON)  # ~5.5 km away
    assert rec.at_home(_HOME_LAT, _HOME_LON, radius_m=200.0) is False


def test_at_home_none_without_home_or_coords() -> None:
    # No home configured.
    rec = _record(latitude=_HOME_LAT, longitude=_HOME_LON)
    assert rec.at_home(None, None, radius_m=200.0) is None
    # No coords in the record.
    rec2 = _record(latitude=None, longitude=None)
    assert rec2.at_home(_HOME_LAT, _HOME_LON, radius_m=200.0) is None


_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_STALE_AFTER = timedelta(minutes=30)


def _confirmed(rec: VehicleRecord) -> bool:
    return rec.at_home_confirmed(_NOW, _STALE_AFTER, _HOME_LAT, _HOME_LON, 200.0)


def test_at_home_confirmed_when_fresh_home_and_plugged() -> None:
    rec = _record(
        latitude=_HOME_LAT,
        longitude=_HOME_LON,
        plugged=True,
        recorded_at=_NOW - timedelta(minutes=5),
    )
    assert _confirmed(rec) is True


def test_at_home_confirmed_false_without_plugged() -> None:
    # The 2026-07-15 failure shape: position frozen on the home fix and SoC
    # updates keeping the record fresh — but the car is unplugged and away.
    frozen_home_fix = _record(
        latitude=_HOME_LAT,
        longitude=_HOME_LON,
        plugged=False,
        recorded_at=_NOW - timedelta(minutes=5),
    )
    assert _confirmed(frozen_home_fix) is False
    # Unknown plugged state fails closed too.
    unknown = _record(
        latitude=_HOME_LAT,
        longitude=_HOME_LON,
        plugged=None,
        recorded_at=_NOW - timedelta(minutes=5),
    )
    assert _confirmed(unknown) is False


def test_at_home_confirmed_false_when_stale() -> None:
    rec = _record(
        latitude=_HOME_LAT,
        longitude=_HOME_LON,
        plugged=True,
        recorded_at=_NOW - timedelta(hours=2),
    )
    assert _confirmed(rec) is False


def test_at_home_confirmed_false_when_away_or_unknown_position() -> None:
    away = _record(
        latitude=_HOME_LAT + 0.05,
        longitude=_HOME_LON,
        plugged=True,
        recorded_at=_NOW - timedelta(minutes=5),
    )
    assert _confirmed(away) is False
    no_fix = _record(
        latitude=None,
        longitude=None,
        plugged=True,
        recorded_at=_NOW - timedelta(minutes=5),
    )
    assert _confirmed(no_fix) is False
