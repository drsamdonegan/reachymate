"""Measured liveness; reading health never renews a heartbeat."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Heartbeat:
    interval_seconds: float = 10.0
    last_success: float | None = None
    last_error: str | None = None

    def beat(self, now: float | None = None) -> None:
        self.last_success = time.monotonic() if now is None else now
        self.last_error = None

    def fail(self, error: str) -> None:
        self.last_error = error

    def healthy(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (
            self.last_error is None
            and self.last_success is not None
            and now - self.last_success < 3 * self.interval_seconds
        )

    def diagnostics(self) -> dict:
        return {
            "healthy": self.healthy(),
            "age_seconds": None
            if self.last_success is None
            else round(time.monotonic() - self.last_success, 3),
            "error": self.last_error,
        }
