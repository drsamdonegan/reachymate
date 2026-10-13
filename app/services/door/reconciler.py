"""Resolve outstanding releases only from authenticated correlated evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.door.door_sensor import DoorSensor, DoorState
from app.storage.release_attempts import UNRESOLVED, ReleaseAttemptRepository


@dataclass(frozen=True)
class LockEntry:
    site_id: str
    lock_id: str
    observed_at: datetime
    access_by: str
    event_category: str
    authenticated: bool = False
    evidence_ref: str | None = None


class Reconciler:
    def __init__(self, *, attempts: ReleaseAttemptRepository, sensor: DoorSensor | None = None) -> None:
        self.attempts = attempts
        self.sensor = sensor

    def reconcile(self, access_event_id: str, *, entry: LockEntry | None = None) -> bool:
        attempt = self.attempts.get(access_event_id)
        if attempt is None or attempt.status not in UNRESOLVED:
            return False
        created = attempt.send_started_at
        if created is None:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        reading = self.sensor.latest() if self.sensor is not None else None
        sensor_ok = (
            reading is not None
            and reading.fresh(lock_id=attempt.lock_id)
            and reading.state == DoorState.OPEN
            and reading.observed_at >= created
        )
        entry_ok = (
            entry is not None
            and entry.authenticated
            and entry.site_id == attempt.site_id
            and entry.lock_id == attempt.lock_id
            and entry.access_by == "remote"
            and entry.event_category == "lock_opened"
            and entry.observed_at.tzinfo is not None
            and created <= entry.observed_at <= datetime.now(timezone.utc)
        )
        if not sensor_ok and not entry_ok:
            return False
        reference = reading.evidence_ref if sensor_ok else entry.evidence_ref
        # PENDING has not begun a send and cannot be confirmed by an unrelated opening.
        if attempt.status == "PENDING":
            return False
        self.attempts.update(
            access_event_id,
            status="ACTUATED_CONFIRMED",
            detail="authenticated_opening_evidence",
            evidence_ref=reference,
        )
        return True
