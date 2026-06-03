from __future__ import annotations

from app.services.audio.barge_in_guard import BargeInGuard


def test_barge_in_guard_blocks_while_engaged() -> None:
    guard = BargeInGuard(cooldown_seconds=1)

    assert guard.can_trigger(robot_speaking=False) is True
    guard.begin_engagement(timeout_seconds=5)
    assert guard.can_trigger(robot_speaking=False) is False
    guard.end_engagement()
    assert guard.can_trigger(robot_speaking=False) is True


def test_barge_in_guard_starts_cooldown() -> None:
    guard = BargeInGuard(cooldown_seconds=5)
    guard.start_cooldown()

    assert guard.can_trigger(robot_speaking=False) is False
