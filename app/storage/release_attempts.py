"""Atomic durable release reservation; no committed attempt means no actuation."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, String, func, select, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.db import Base
from app.storage.tables import AccessEventRecord

UNRESOLVED = {"PENDING", "SENDING", "ACCEPTED_UNCONFIRMED", "UNCERTAIN"}
TERMINAL = {"ACTUATED_CONFIRMED", "REJECTED", "FAILED"}


class ReleaseAttemptRecord(Base):
    __tablename__ = "release_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    access_event_id: Mapped[str] = mapped_column(String(36), ForeignKey("access_events.id"), unique=True)
    device_id: Mapped[str] = mapped_column(String(128))
    site_id: Mapped[str] = mapped_column(String(128))
    lock_id: Mapped[str] = mapped_column(String(128))
    stage: Mapped[str] = mapped_column(String(2))
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ReleaseAttemptRepository:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self._lock = threading.Lock()

    def get(self, access_event_id: str) -> ReleaseAttemptRecord | None:
        with self.session_factory() as session:
            return session.scalar(
                select(ReleaseAttemptRecord).where(ReleaseAttemptRecord.access_event_id == access_event_id)
            )

    def reserve(
        self, *, access_event_id: str, device_id: str, site_id: str, lock_id: str, stage: str
    ) -> tuple[ReleaseAttemptRecord | None, str]:
        now = datetime.now(timezone.utc)
        with self._lock, self.session_factory() as session:
            try:
                # SQLite serializes the check-and-reserve across processes.
                if session.bind.dialect.name == "sqlite":
                    session.connection(execution_options={"sqlite_begin_mode": "IMMEDIATE"})
                elif session.bind.dialect.name == "postgresql":
                    session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:lock))"),
                        {"lock": site_id + ":" + lock_id},
                    )
                else:
                    return None, "unsupported_reservation_database"
                event = session.get(AccessEventRecord, access_event_id)
                if event is None:
                    return None, "missing_access_event"
                if event.decision != "ALLOW" or event.reason_code != "ALLOW_ACTIVE_BOOKING":
                    return None, "durable_policy_not_allow"
                old = session.scalar(
                    select(ReleaseAttemptRecord).where(
                        ReleaseAttemptRecord.access_event_id == access_event_id
                    )
                )
                if old is not None:
                    return old, "duplicate_suppressed"
                unresolved = session.scalar(
                    select(ReleaseAttemptRecord.id)
                    .where(
                        ReleaseAttemptRecord.site_id == site_id,
                        ReleaseAttemptRecord.lock_id == lock_id,
                        ReleaseAttemptRecord.status.in_(UNRESOLVED),
                    )
                    .limit(1)
                )
                if unresolved is not None:
                    return None, "unresolved_release_blocks_lock"
                recent = session.scalar(
                    select(func.count())
                    .select_from(ReleaseAttemptRecord)
                    .where(
                        ReleaseAttemptRecord.site_id == site_id,
                        ReleaseAttemptRecord.lock_id == lock_id,
                        ReleaseAttemptRecord.created_at >= now - timedelta(seconds=10),
                    )
                )
                hourly = session.scalar(
                    select(func.count())
                    .select_from(ReleaseAttemptRecord)
                    .where(
                        ReleaseAttemptRecord.site_id == site_id,
                        ReleaseAttemptRecord.lock_id == lock_id,
                        ReleaseAttemptRecord.created_at >= now - timedelta(hours=1),
                    )
                )
                if recent or hourly >= 30:
                    return None, "release_rate_limited"
                row = ReleaseAttemptRecord(
                    id=str(uuid4()),
                    access_event_id=access_event_id,
                    device_id=device_id,
                    site_id=site_id,
                    lock_id=lock_id,
                    stage=stage,
                    status="PENDING",
                    detail="durable_reservation",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.commit()
                return row, "reserved"
            except Exception:
                session.rollback()
                return None, "reservation_failed"

    def update(
        self,
        access_event_id: str,
        *,
        status: str,
        detail: str,
        provider_request_id: str | None = None,
        evidence_ref: str | None = None,
    ) -> ReleaseAttemptRecord:
        allowed = {
            "PENDING": {"SENDING", "REJECTED", "FAILED", "UNCERTAIN"},
            "SENDING": {"ACTUATED_CONFIRMED", "ACCEPTED_UNCONFIRMED", "REJECTED", "FAILED", "UNCERTAIN"},
            "ACCEPTED_UNCONFIRMED": {"ACTUATED_CONFIRMED", "FAILED", "UNCERTAIN"},
            "UNCERTAIN": {"ACTUATED_CONFIRMED", "FAILED"},
        }
        with self._lock, self.session_factory() as session:
            if session.bind.dialect.name == "sqlite":
                session.connection(execution_options={"sqlite_begin_mode": "IMMEDIATE"})
            row = session.scalar(
                select(ReleaseAttemptRecord)
                .where(ReleaseAttemptRecord.access_event_id == access_event_id)
                .with_for_update()
            )
            if row is None:
                raise ValueError("Release attempt is missing")
            if status != row.status and status not in allowed.get(row.status, set()):
                raise ValueError("Release transition is not permitted")
            row.status, row.detail, row.updated_at = status, detail[:128], datetime.now(timezone.utc)
            if status == "SENDING" and row.send_started_at is None:
                row.send_started_at = row.updated_at
            if provider_request_id is not None:
                row.provider_request_id = provider_request_id[:128]
            if evidence_ref is not None:
                row.evidence_ref = evidence_ref[:128]
            session.commit()
            return row
