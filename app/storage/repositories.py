"""Repository objects wrapping SQLAlchemy access."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.domain import BookingResult, FaceMatchResult, FaceQuality, PolicyDecision, UnlockResult
from app.models.enums import BookingStatus, Decision, FaceQualityFlag, HealthStatus

from .tables import AccessEventRecord, FaceEmbeddingRecord, SaltoTokenRecord, SystemHealthRecord, UserRecord


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self,
        *,
        user_id: str | None = None,
        full_name: str,
        slack_user_id: str,
        slack_display_name: str,
        company: str | None = None,
        active: bool = True,
    ) -> UserRecord:
        record = self.get_by_slack_id(slack_user_id)
        if record is None and user_id:
            record = self.get(user_id)
        if record is None:
            payload = {
                "full_name": full_name,
                "slack_user_id": slack_user_id,
                "slack_display_name": slack_display_name,
                "company": company,
                "active": active,
            }
            if user_id:
                payload["id"] = user_id
            record = UserRecord(**payload)
            self.session.add(record)
            self.session.flush()
            return record

        record.full_name = full_name
        record.slack_user_id = slack_user_id
        record.slack_display_name = slack_display_name
        record.company = company
        record.active = active
        self.session.flush()
        return record

    def get(self, user_id: str) -> UserRecord | None:
        return self.session.get(UserRecord, user_id)

    def get_by_slack_id(self, slack_user_id: str) -> UserRecord | None:
        return self.session.scalar(select(UserRecord).where(UserRecord.slack_user_id == slack_user_id))

    def list(self) -> list[UserRecord]:
        return list(self.session.scalars(select(UserRecord).order_by(UserRecord.created_at.desc())))


class SqlFaceStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace_embeddings(self, user_id: str, embeddings: list[tuple[list[float], float, str | None]]) -> None:
        self.session.execute(delete(FaceEmbeddingRecord).where(FaceEmbeddingRecord.user_id == user_id))
        for vector, quality_score, label in embeddings:
            self.session.add(
                FaceEmbeddingRecord(
                    user_id=user_id,
                    embedding_vector=vector,
                    quality_score=quality_score,
                    sample_label=label,
                )
            )
        self.session.flush()

    def list_embeddings(self) -> dict[str, list[list[float]]]:
        result: dict[str, list[list[float]]] = {}
        rows = self.session.scalars(select(FaceEmbeddingRecord).order_by(FaceEmbeddingRecord.created_at.asc()))
        for row in rows:
            result.setdefault(row.user_id, []).append([float(value) for value in row.embedding_vector])
        return result


class AccessEventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        face_match: FaceMatchResult,
        booking_result: BookingResult,
        decision: PolicyDecision,
        unlock_result: UnlockResult | None,
        latency_ms: int,
        metadata: dict[str, Any],
    ) -> AccessEventRecord:
        record = AccessEventRecord(
            detected_user_id=face_match.matched_user_id,
            matched_confidence=face_match.similarity,
            booking_status=booking_result.status.value,
            face_quality={
                "usable": face_match.quality.usable,
                "flags": [flag.value for flag in face_match.quality.flags],
                "face_count": face_match.quality.face_count,
                "face_size": face_match.quality.face_size,
                "blur_score": face_match.quality.blur_score,
                "brightness": face_match.quality.brightness,
            },
            decision=decision.decision.value,
            reason_code=decision.reason_code.value,
            salto_result=unlock_result.detail if unlock_result else None,
            latency_ms=latency_ms,
            metadata_json=metadata,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def list_recent(self, limit: int = 50) -> list[AccessEventRecord]:
        query = select(AccessEventRecord).order_by(AccessEventRecord.timestamp.desc()).limit(limit)
        return list(self.session.scalars(query))


class SystemHealthRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def set_status(self, component: str, status: HealthStatus, details: dict[str, Any] | None = None) -> None:
        record = self.session.get(SystemHealthRecord, component)
        if record is None:
            record = SystemHealthRecord(component=component, status=status.value, details_json=details or {})
            self.session.add(record)
        else:
            record.status = status.value
            record.details_json = details or {}
            record.updated_at = datetime.utcnow()
        self.session.flush()

    def snapshot(self) -> dict[str, HealthStatus]:
        rows = self.session.scalars(select(SystemHealthRecord))
        return {row.component: HealthStatus(row.status) for row in rows}

    def list(self) -> list[SystemHealthRecord]:
        return list(self.session.scalars(select(SystemHealthRecord).order_by(SystemHealthRecord.component.asc())))


class SaltoTokenRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, provider: str = "salto_ks") -> SaltoTokenRecord | None:
        return self.session.scalar(select(SaltoTokenRecord).where(SaltoTokenRecord.provider == provider))

    def save(
        self,
        *,
        access_token: str,
        refresh_token: str | None,
        expires_at: datetime | None,
        token_type: str = "Bearer",
        provider: str = "salto_ks",
    ) -> SaltoTokenRecord:
        record = self.get(provider=provider)
        if record is None:
            record = SaltoTokenRecord(
                provider=provider,
                access_token=access_token,
                refresh_token=refresh_token,
                token_type=token_type,
                expires_at=expires_at,
            )
            self.session.add(record)
        else:
            record.access_token = access_token
            record.refresh_token = refresh_token
            record.token_type = token_type
            record.expires_at = expires_at
            record.updated_at = datetime.utcnow()
        self.session.flush()
        return record
