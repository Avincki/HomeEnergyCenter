"""In-memory holder for the current manual override.

Lives for the lifetime of the FastAPI app process. Survives DB reads/writes
but does not persist across restarts — overrides are intentionally
short-term per spec ("force ON / OFF for a configurable duration").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from energy_orchestrator.data.models import OverrideMode
from energy_orchestrator.decision import OverrideState


class OverrideController:
    def __init__(self) -> None:
        self._state: OverrideState | None = None

    def get_active(self, now: datetime | None = None) -> OverrideState | None:
        """Return the active override (auto-clearing if expired) or None."""
        current = now or datetime.now(UTC)
        if self._state is None:
            return None
        if not self._state.is_active(current):
            self._state = None
            return None
        return self._state

    def set(self, mode: OverrideMode, minutes: int | None = None) -> OverrideState | None:
        """Set or clear the override. Passing ``mode=AUTO`` clears any existing override."""
        if mode is OverrideMode.AUTO:
            self._state = None
            return None
        expires_at: datetime | None = None
        if minutes is not None:
            expires_at = datetime.now(UTC) + timedelta(minutes=minutes)
        self._state = OverrideState(mode=mode, expires_at=expires_at)
        return self._state

    def clear(self) -> None:
        self._state = None
