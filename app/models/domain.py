"""Pure domain dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .enums import AppState, BookingStatus, Decision, FaceQualityFlag, HealthStatus, ReasonCode

BoundingBox = tuple[int, int, int, int]


@dataclass(slots=True)
class FaceQuality:
    usable: bool
    flags: list[FaceQualityFlag] = field(default_factory=list)
    face_count: int = 0
    face_size: int = 0
    blur_score: float = 0.0
    brightness: float = 0.0


@dataclass(slots=True)
class FaceMatchResult:
    matched_user_id: str | None
    similarity: float | None
    quality: FaceQuality
    bbox: BoundingBox | None = None
    stable_frames: int = 0
    embedding: list[float] | None = None
    match_status: str | None = None
    margin: float | None = None
    score_band: str | None = None


@dataclass(slots=True)
class BookingResult:
    status: BookingStatus
    active: bool
    booking_date: date | None = None
    source_booking_id: str | None = None
    detail: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SystemStatusSnapshot:
    ready: bool
    components: dict[str, HealthStatus] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: Decision
    reason_code: ReasonCode
    allowed: bool
    message: str


@dataclass(slots=True)
class UnlockResult:
    success: bool
    detail: str | None = None
    provider_request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AccessAttempt:
    access_event_id: str
    started_at: datetime
    state: AppState
