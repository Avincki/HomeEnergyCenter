"""Display timezone — the single source of truth for user-facing timestamps.

Internal storage and computation stay in UTC (the orchestrator's tick clock,
the SQLite rows, the price/solar windows, the sunrise/sunset comparison). Only
*rendering* — logs, server-rendered pages — is converted to this zone, so the
operator reads Brussels wall-clock time without the codebase juggling local
time internally.

The system runs at one fixed location, so the zone is a constant rather than a
config knob; change it here if the install ever moves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Brussels")


def to_local(dt: datetime) -> datetime:
    """Convert ``dt`` to ``LOCAL_TZ``, treating naive datetimes as UTC.

    SQLite drops tzinfo on round-trip even with ``DateTime(timezone=True)``, so
    values read back are naive-but-UTC; normalise them before converting.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(LOCAL_TZ)


def now_local() -> datetime:
    """Current instant in ``LOCAL_TZ``."""
    return datetime.now(LOCAL_TZ)
