"""Repository objects wrapping SQLAlchemy access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.domain import BookingResult, FaceMatchResult, PolicyDecision, UnlockResult
from app.models.enums import HealthStatus
from app.storage.encryption import SensitiveDataCipher

from .tables import (
    AccessEventRecord,
    BiometricConsentRecord,
    FaceEmbeddingRecord,
    SaltoTokenRecord,
    SystemHealthRecord,
    UnlockCommandRecord,
    UserRecord,
)


@dataclass(frozen=True)
class SaltoTokenView:
    provider: str
    access_token: str
    refresh_token: str | None
    token_type: str
    expires_at: datetime | None


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
        biometric_consent: bool = False,
        access_revoked: bool | None = None,
        greet_by_name: bool = False,
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
                "biometric_consent": biometric_consent,
                "access_revoked": bool(access_revoked),
                "greet_by_name": greet_by_name,
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
        record.biometric_consent = biometric_consent
        if access_revoked is not None:
            record.access_revoked = access_revoked
        record.greet_by_name = greet_by_name
        self.session.flush()
        return record

    def get(self, user_id: str) -> UserRecord | None:
        return self.session.get(UserRecord, user_id)

    def get_by_slack_id(self, slack_user_id: str) -> UserRecord | None:
        return self.session.scalar(select(UserRecord).where(UserRecord.slack_user_id == slack_user_id))

    def list(self) -> list[UserRecord]:
        return list(self.session.scalars(select(UserRecord).order_by(UserRecord.created_at.desc())))

    def reinstate(self, user_id: str) -> UserRecord:
        user = self.get(user_id)
        if user is None:
            raise KeyError(user_id)
        user.access_revoked = False
        self.session.flush()
        return user

    def revoke(self, user_id: str, *, withdraw_consent: bool = True) -> None:
        user = self.get(user_id)
        if user is None:
            raise KeyError(user_id)
        user.access_revoked = True
        if withdraw_consent:
            user.biometric_consent = False
            self.session.execute(delete(FaceEmbeddingRecord).where(FaceEmbeddingRecord.user_id == user_id))
            for consent in self.session.scalars(
                select(BiometricConsentRecord).where(
                    BiometricConsentRecord.user_id == user_id, BiometricConsentRecord.withdrawn_at.is_(None)
                )
            ):
                consent.withdrawn_at = datetime.now(timezone.utc)
        self.session.flush()

    def delete(self, user_id: str) -> None:
        user = self.get(user_id)
        if user is None:
            raise KeyError(user_id)
        self.session.delete(user)
        self.session.flush()

    def end_membership(self, user_id: str) -> None:
        self.revoke(user_id)
        self.get(user_id).active = False
        self.session.flush()

    def export(self, user_id: str) -> dict[str, Any]:
        user = self.get(user_id)
        if user is None:
            raise KeyError(user_id)
        consents = list(
            self.session.scalars(
                select(BiometricConsentRecord).where(BiometricConsentRecord.user_id == user_id)
            )
        )
        templates = list(
            self.session.scalars(select(FaceEmbeddingRecord).where(FaceEmbeddingRecord.user_id == user_id))
        )
        return {
            "user": {
                name: getattr(user, name)
                for name in (
                    "id",
                    "full_name",
                    "slack_user_id",
                    "slack_display_name",
                    "company",
                    "active",
                    "biometric_consent",
                    "access_revoked",
                    "greet_by_name",
                )
            },
            "consents": [
                {
                    name: getattr(row, name)
                    for name in (
                        "notice_version",
                        "notice_sha256",
                        "granted_at",
                        "method",
                        "operator_id",
                        "withdrawn_at",
                    )
                }
                for row in consents
            ],
            "templates": [
                {
                    "id": row.id,
                    "model_id": row.model_id,
                    "model_sha256": row.model_sha256,
                    "capture_source": row.capture_source,
                    "encrypted_embedding": row.embedding_vector,
                }
                for row in templates
            ],
        }


class SqlFaceStore:
    def __init__(
        self,
        session: Session,
        *,
        cipher: SensitiveDataCipher | None = None,
        allow_plaintext: bool = True,
    ) -> None:
        self.session = session
        self.cipher = cipher or SensitiveDataCipher(None)
        self.allow_plaintext = allow_plaintext

    def replace_embeddings(
        self,
        user_id: str,
        embeddings: list[tuple[list[float], float, str | None]],
        *,
        model_id: str = "sface-2021dec",
        model_sha256: str | None = None,
        capture_source: str = "legacy",
    ) -> None:
        if not self.cipher.enabled and not self.allow_plaintext:
            raise RuntimeError("DATA_ENCRYPTION_KEY is required for face templates")
        if len(embeddings) > 20:
            raise ValueError("At most 20 face templates are allowed")
        self.session.execute(delete(FaceEmbeddingRecord).where(FaceEmbeddingRecord.user_id == user_id))
        for vector, quality_score, label in embeddings:
            row_id = str(uuid4())
            self.session.add(
                FaceEmbeddingRecord(
                    id=row_id,
                    user_id=user_id,
                    embedding_vector=self.cipher.encrypt_json(
                        vector,
                        purpose=face_embedding_purpose(user_id, row_id),
                    ),
                    quality_score=quality_score,
                    sample_label=label,
                    model_id=model_id,
                    model_sha256=model_sha256,
                    capture_source=capture_source,
                )
            )
        self.session.flush()

    def enroll_atomic(
        self,
        *,
        full_name: str,
        slack_user_id: str,
        slack_display_name: str,
        embeddings: list[tuple[list[float], float, str | None]],
        operator_id: str,
        notice_version: str,
        notice_sha256: str,
        company: str | None = None,
        consent_method: str = "member_checkbox",
        model_id: str = "sface-2021dec",
        model_sha256: str | None = None,
        capture_source: str = "file",
        greet_by_name: bool = False,
        reinstate: bool = False,
    ) -> UserRecord:
        if not self.cipher.enabled:
            raise RuntimeError("DATA_ENCRYPTION_KEY is required before enrollment")
        if not embeddings or len(embeddings) > 20:
            raise ValueError("Enrollment requires 1 to 20 validated templates")
        if not operator_id.strip() or not notice_version.strip() or not consent_method.strip():
            raise ValueError("Recorded consent operator, notice and method are required")
        if len(notice_sha256) != 64 or any(c not in "0123456789abcdef" for c in notice_sha256):
            raise ValueError("Consent notice SHA256 is required")
        from math import isfinite

        dimensions = {len(vector) for vector, _, _ in embeddings}
        if (
            len(dimensions) != 1
            or not next(iter(dimensions))
            or any(not isfinite(float(value)) for vector, _, _ in embeddings for value in vector)
        ):
            raise ValueError("All templates must be finite vectors from the same model")
        existing = UserRepository(self.session).get_by_slack_id(slack_user_id)
        if existing is not None and existing.access_revoked and not reinstate:
            raise ValueError("Member access is revoked; explicit --reinstate is required")
        # A savepoint rolls back user, consent and template updates together,
        # even when called inside a longer admin transaction.
        with self.session.begin_nested():
            if existing is None:
                user = UserRepository(self.session).upsert(
                    full_name=full_name,
                    slack_user_id=slack_user_id,
                    slack_display_name=slack_display_name,
                    company=company,
                    biometric_consent=True,
                    greet_by_name=greet_by_name,
                )
            else:
                user = existing
                user.full_name, user.slack_display_name, user.company = full_name, slack_display_name, company
                user.biometric_consent, user.greet_by_name = True, greet_by_name
                if reinstate:
                    UserRepository(self.session).reinstate(user.id)
            self.replace_embeddings(
                user.id,
                embeddings,
                model_id=model_id,
                model_sha256=model_sha256,
                capture_source=capture_source,
            )
            self.session.add(
                BiometricConsentRecord(
                    user_id=user.id,
                    notice_version=notice_version,
                    notice_sha256=notice_sha256,
                    method=consent_method,
                    operator_id=operator_id,
                )
            )
            self.session.flush()
        return user

    def list_embeddings(self, *, model_id: str | None = None) -> dict[str, list[list[float]]]:
        result: dict[str, list[list[float]]] = {}
        rows = self.session.scalars(
            select(FaceEmbeddingRecord)
            .join(UserRecord, UserRecord.id == FaceEmbeddingRecord.user_id)
            .where(
                UserRecord.active.is_(True),
                UserRecord.biometric_consent.is_(True),
                UserRecord.access_revoked.is_(False),
                select(BiometricConsentRecord.id)
                .where(
                    BiometricConsentRecord.user_id == UserRecord.id,
                    BiometricConsentRecord.withdrawn_at.is_(None),
                )
                .exists(),
            )
            .order_by(FaceEmbeddingRecord.created_at.asc())
        )
        for row in rows:
            if model_id is not None and row.model_id != model_id:
                continue
            vector = self.cipher.decrypt_json(
                row.embedding_vector,
                purpose=face_embedding_purpose(row.user_id, row.id),
                allow_plaintext=self.allow_plaintext,
            )
            if not isinstance(vector, list):
                raise RuntimeError("Face embedding record is malformed")
            result.setdefault(row.user_id, []).append([float(value) for value in vector])
        return result


def face_embedding_purpose(user_id: str, row_id: str) -> str:
    return f"face_embedding|{user_id}|{row_id}"


class AccessEventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        access_event_id: str | None = None,
        face_match: FaceMatchResult,
        booking_result: BookingResult,
        decision: PolicyDecision,
        unlock_result: UnlockResult | None,
        latency_ms: int,
        metadata: dict[str, Any],
    ) -> AccessEventRecord:
        record = AccessEventRecord(
            id=access_event_id,
            detected_user_id=face_match.matched_user_id,
            matched_confidence=None,
            booking_status=booking_result.status.value,
            face_quality={
                "flags": [flag.value for flag in face_match.quality.flags],
                "score_band": face_match.score_band,
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

    def update(
        self,
        *,
        access_event_id: str,
        face_match: FaceMatchResult,
        booking_result: BookingResult,
        decision: PolicyDecision,
        unlock_result: UnlockResult | None,
        latency_ms: int,
        metadata: dict[str, Any],
    ) -> AccessEventRecord:
        record = self.session.get(AccessEventRecord, access_event_id)
        if record is None:
            raise KeyError(access_event_id)
        record.detected_user_id = face_match.matched_user_id
        record.matched_confidence = None
        record.booking_status = booking_result.status.value
        record.face_quality = {
            "flags": [flag.value for flag in face_match.quality.flags],
            "score_band": face_match.score_band,
        }
        record.decision = decision.decision.value
        record.reason_code = decision.reason_code.value
        record.salto_result = unlock_result.detail if unlock_result else None
        record.latency_ms = latency_ms
        record.metadata_json = metadata
        self.session.flush()
        return record

    def list_recent(self, limit: int = 50) -> list[AccessEventRecord]:
        query = select(AccessEventRecord).order_by(AccessEventRecord.timestamp.desc()).limit(limit)
        return list(self.session.scalars(query))


class UnlockCommandRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_for_event(self, access_event_id: str) -> UnlockCommandRecord | None:
        return self.session.scalar(
            select(UnlockCommandRecord).where(UnlockCommandRecord.access_event_id == access_event_id)
        )

    def reserve(self, access_event_id: str) -> tuple[UnlockCommandRecord, bool]:
        existing = self.get_for_event(access_event_id)
        if existing is not None:
            return existing, False
        record = UnlockCommandRecord(access_event_id=access_event_id, status="PENDING")
        self.session.add(record)
        self.session.flush()
        return record, True

    def update(
        self,
        access_event_id: str,
        *,
        status: str,
        detail: str | None = None,
        provider_request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UnlockCommandRecord:
        record = self.get_for_event(access_event_id)
        if record is None:
            raise KeyError(access_event_id)
        record.status = status
        record.detail = detail
        record.provider_request_id = provider_request_id
        record.metadata_json = metadata or {}
        record.updated_at = datetime.utcnow()
        self.session.flush()
        return record


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

    def snapshot(self, *, max_age_seconds: int | None = None) -> dict[str, HealthStatus]:
        rows = self.session.scalars(select(SystemHealthRecord))
        now = datetime.now(timezone.utc)
        snapshot: dict[str, HealthStatus] = {}
        for row in rows:
            updated_at = row.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if max_age_seconds is not None and updated_at < now - timedelta(seconds=max_age_seconds):
                snapshot[row.component] = HealthStatus.ERROR
            else:
                snapshot[row.component] = HealthStatus(row.status)
        return snapshot

    def list(self) -> list[SystemHealthRecord]:
        return list(
            self.session.scalars(select(SystemHealthRecord).order_by(SystemHealthRecord.component.asc()))
        )


class SaltoTokenRepository:
    def __init__(
        self,
        session: Session,
        *,
        cipher: SensitiveDataCipher | None = None,
        allow_plaintext: bool = True,
    ) -> None:
        self.session = session
        self.cipher = cipher or SensitiveDataCipher(None)
        self.allow_plaintext = allow_plaintext

    def _get_record(self, provider: str) -> SaltoTokenRecord | None:
        return self.session.scalar(select(SaltoTokenRecord).where(SaltoTokenRecord.provider == provider))

    def get(self, provider: str = "salto_ks") -> SaltoTokenView | None:
        record = self._get_record(provider)
        if record is None:
            return None
        return SaltoTokenView(
            provider=record.provider,
            access_token=self.cipher.decrypt_text(
                record.access_token,
                purpose="salto_access_token",
                allow_plaintext=self.allow_plaintext,
            )
            or "",
            refresh_token=self.cipher.decrypt_text(
                record.refresh_token,
                purpose="salto_refresh_token",
                allow_plaintext=self.allow_plaintext,
            ),
            token_type=record.token_type,
            expires_at=record.expires_at,
        )

    def save(
        self,
        *,
        access_token: str,
        refresh_token: str | None,
        expires_at: datetime | None,
        token_type: str = "Bearer",
        provider: str = "salto_ks",
    ) -> SaltoTokenRecord:
        record = self._get_record(provider)
        if record is None:
            record = SaltoTokenRecord(
                provider=provider,
                access_token=self.cipher.encrypt_text(access_token, purpose="salto_access_token"),
                refresh_token=self.cipher.encrypt_text(refresh_token, purpose="salto_refresh_token"),
                token_type=token_type,
                expires_at=expires_at,
            )
            self.session.add(record)
        else:
            record.access_token = self.cipher.encrypt_text(access_token, purpose="salto_access_token") or ""
            record.refresh_token = self.cipher.encrypt_text(refresh_token, purpose="salto_refresh_token")
            record.token_type = token_type
            record.expires_at = expires_at
            record.updated_at = datetime.utcnow()
        self.session.flush()
        return record
