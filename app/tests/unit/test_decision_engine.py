from __future__ import annotations

from app.models.domain import BookingResult, FaceMatchResult, FaceQuality, SystemStatusSnapshot
from app.models.enums import BookingStatus, Decision, FaceQualityFlag, HealthStatus, ReasonCode
from app.services.policy.decision_engine import DecisionEngine


def test_decision_engine_allows_known_face_with_active_booking(known_face_result: FaceMatchResult) -> None:
    engine = DecisionEngine(face_match_threshold=0.36)

    decision = engine.evaluate(
        face_match_result=known_face_result,
        booking_result=BookingResult(status=BookingStatus.ACTIVE, active=True),
        system_status=SystemStatusSnapshot(ready=True, components={"database": HealthStatus.HEALTHY}),
    )

    assert decision.allowed is True
    assert decision.decision == Decision.ALLOW
    assert decision.reason_code == ReasonCode.ALLOW_ACTIVE_BOOKING


def test_decision_engine_denies_multiple_faces() -> None:
    engine = DecisionEngine(face_match_threshold=0.36)
    face_result = FaceMatchResult(
        matched_user_id=None,
        similarity=None,
        quality=FaceQuality(
            usable=False,
            flags=[FaceQualityFlag.MULTIPLE_FACES],
            face_count=2,
            face_size=120,
            blur_score=130,
            brightness=150,
        ),
    )

    decision = engine.evaluate(
        face_match_result=face_result,
        booking_result=BookingResult(status=BookingStatus.NONE, active=False),
        system_status=SystemStatusSnapshot(ready=True),
    )

    assert decision.allowed is False
    assert decision.reason_code == ReasonCode.DENY_MULTIPLE_FACES


def test_decision_engine_denies_when_booking_lookup_fails(known_face_result: FaceMatchResult) -> None:
    engine = DecisionEngine(face_match_threshold=0.36)

    decision = engine.evaluate(
        face_match_result=known_face_result,
        booking_result=BookingResult(status=BookingStatus.LOOKUP_FAILED, active=False),
        system_status=SystemStatusSnapshot(ready=True),
    )

    assert decision.allowed is False
    assert decision.reason_code == ReasonCode.DENY_BOOKING_LOOKUP_FAILED


def test_decision_engine_fails_closed_on_system_health(known_face_result: FaceMatchResult) -> None:
    engine = DecisionEngine(face_match_threshold=0.36)

    decision = engine.evaluate(
        face_match_result=known_face_result,
        booking_result=BookingResult(status=BookingStatus.ACTIVE, active=True),
        system_status=SystemStatusSnapshot(ready=False, components={"database": HealthStatus.ERROR}),
    )

    assert decision.allowed is False
    assert decision.decision == Decision.ERROR
    assert decision.reason_code == ReasonCode.ERROR_INTERNAL
