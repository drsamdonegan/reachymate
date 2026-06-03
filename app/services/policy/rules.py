"""Pure policy helper rules."""

from __future__ import annotations

from app.models.domain import BookingResult, FaceMatchResult, SystemStatusSnapshot
from app.models.enums import BookingStatus, FaceQualityFlag, ReasonCode


def system_can_authorize(system_status: SystemStatusSnapshot) -> bool:
    """Return whether required services are healthy enough to make an auth decision."""

    return system_status.ready


def face_reason(face_match: FaceMatchResult, face_match_threshold: float) -> ReasonCode | None:
    """Return a denial reason for face-related failure, otherwise None."""

    if face_match.quality.face_count == 0 or FaceQualityFlag.NO_FACE in face_match.quality.flags:
        return ReasonCode.DENY_NO_FACE
    if face_match.quality.face_count > 1 or FaceQualityFlag.MULTIPLE_FACES in face_match.quality.flags:
        return ReasonCode.DENY_MULTIPLE_FACES
    if FaceQualityFlag.FACE_TOO_SMALL in face_match.quality.flags:
        return ReasonCode.DENY_FACE_TOO_SMALL
    if not face_match.quality.usable:
        return ReasonCode.DENY_LOW_FACE_QUALITY
    if face_match.matched_user_id is None and face_match.similarity is None:
        return ReasonCode.DENY_UNKNOWN_FACE
    if face_match.matched_user_id is None:
        return ReasonCode.DENY_LOW_MATCH_CONFIDENCE
    if face_match.similarity is None or face_match.similarity < face_match_threshold:
        return ReasonCode.DENY_LOW_MATCH_CONFIDENCE
    return None


def booking_reason(booking_result: BookingResult) -> ReasonCode | None:
    """Return booking-related deny reason if any."""

    if booking_result.status == BookingStatus.LOOKUP_FAILED:
        return ReasonCode.DENY_BOOKING_LOOKUP_FAILED
    if not booking_result.active:
        return ReasonCode.DENY_NO_ACTIVE_BOOKING
    return None
