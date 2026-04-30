from __future__ import annotations

from datetime import UTC, datetime, timedelta

from energy_orchestrator.data.models import OverrideMode
from energy_orchestrator.web.override import OverrideController


def test_initial_state_no_override() -> None:
    controller = OverrideController()
    assert controller.get_active() is None


def test_set_force_on_indefinite() -> None:
    controller = OverrideController()
    state = controller.set(OverrideMode.FORCE_ON)
    assert state is not None
    assert state.mode is OverrideMode.FORCE_ON
    assert state.expires_at is None
    assert controller.get_active() is state


def test_set_force_off_with_expiry() -> None:
    controller = OverrideController()
    state = controller.set(OverrideMode.FORCE_OFF, minutes=30)
    assert state is not None
    assert state.expires_at is not None
    # Roughly 30 minutes from now.
    delta = state.expires_at - datetime.now(UTC)
    assert timedelta(minutes=29) < delta <= timedelta(minutes=31)


def test_auto_clears_existing_override() -> None:
    controller = OverrideController()
    controller.set(OverrideMode.FORCE_ON)
    cleared = controller.set(OverrideMode.AUTO)
    assert cleared is None
    assert controller.get_active() is None


def test_clear_method() -> None:
    controller = OverrideController()
    controller.set(OverrideMode.FORCE_ON)
    controller.clear()
    assert controller.get_active() is None


def test_get_active_auto_clears_expired() -> None:
    controller = OverrideController()
    controller.set(OverrideMode.FORCE_ON, minutes=1)
    # Pretend "now" is 5 minutes from when set was called.
    future = datetime.now(UTC) + timedelta(minutes=5)
    assert controller.get_active(future) is None
    # Subsequent calls (without explicit `now`) should also see no override.
    assert controller.get_active() is None
