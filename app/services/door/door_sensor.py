"""Authenticated, time-bounded door sensor evidence; no GPIO is composed here."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol


class DoorState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DoorReading:
    lock_id: str
    state: DoorState
    observed_at: datetime
    authenticated: bool = False
    evidence_ref: str | None = None

    def fresh(self, *, lock_id: str, max_age_seconds: float = 2) -> bool:
        if self.observed_at.tzinfo is None:
            return False
        age = (datetime.now(timezone.utc) - self.observed_at).total_seconds()
        return self.authenticated and self.lock_id == lock_id and 0 <= age <= max_age_seconds


class DoorSensor(Protocol):
    def latest(self) -> DoorReading | None: ...
