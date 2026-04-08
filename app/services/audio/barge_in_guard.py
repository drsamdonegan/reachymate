"""Speech and cooldown guard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class BargeInGuard:
    """Suppress wake triggers during speech and cooldown windows."""

    def __init__(self, cooldown_seconds: int) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._cooldown_until: datetime | None = None
        self._engaged_until: datetime | None = None

    def begin_engagement(self, timeout_seconds: int) -> None:
        self._engaged_until = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)

    def end_engagement(self) -> None:
        self._engaged_until = None

    def is_engaged(self) -> bool:
        return self._engaged_until is not None and datetime.now(timezone.utc) < self._engaged_until

    def start_cooldown(self) -> None:
        self._cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=self.cooldown_seconds)

    def can_trigger(self, *, robot_speaking: bool) -> bool:
        now = datetime.now(timezone.utc)
        if robot_speaking:
            return False
        if self._cooldown_until and now < self._cooldown_until:
            return False
        if self.is_engaged():
            return False
        return True
