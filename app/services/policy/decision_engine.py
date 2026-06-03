"""Decision engine."""

from __future__ import annotations

from app.models.domain import BookingResult, FaceMatchResult, PolicyDecision, SystemStatusSnapshot
from app.models.enums import Decision, ReasonCode
from app.services.policy.rules import booking_reason, face_reason, system_can_authorize


class DecisionEngine:
    """Evaluate the allow/deny policy."""

    def __init__(self, *, face_match_threshold: float) -> None:
        self.face_match_threshold = face_match_threshold

    def evaluate(
        self,
        *,
        face_match_result: FaceMatchResult,
        booking_result: BookingResult,
        system_status: SystemStatusSnapshot,
    ) -> PolicyDecision:
        """Return the final policy decision."""

        if not system_can_authorize(system_status):
            return PolicyDecision(
                decision=Decision.ERROR,
                reason_code=ReasonCode.ERROR_INTERNAL,
                allowed=False,
                message="System health degraded. Access checks are temporarily unavailable.",
            )

        failure = face_reason(face_match_result, self.face_match_threshold)
        if failure is not None:
            return PolicyDecision(
                decision=Decision.DENY,
                reason_code=failure,
                allowed=False,
                message=_message_for_reason(failure),
            )

        failure = booking_reason(booking_result)
        if failure is not None:
            return PolicyDecision(
                decision=Decision.DENY,
                reason_code=failure,
                allowed=False,
                message=_message_for_reason(failure),
            )

        return PolicyDecision(
            decision=Decision.ALLOW,
            reason_code=ReasonCode.ALLOW_ACTIVE_BOOKING,
            allowed=True,
            message=_message_for_reason(ReasonCode.ALLOW_ACTIVE_BOOKING),
        )


def _message_for_reason(reason: ReasonCode) -> str:
    match reason:
        case ReasonCode.ALLOW_ACTIVE_BOOKING:
            return "Welcome. Opening the door."
        case ReasonCode.DENY_NO_FACE:
            return "I can't see a usable face yet. Please step in front of me."
        case ReasonCode.DENY_MULTIPLE_FACES:
            return "I can only help one person at a time. Please step in front of me one at a time."
        case ReasonCode.DENY_FACE_TOO_SMALL:
            return "Please stand closer so I can identify you safely."
        case ReasonCode.DENY_LOW_FACE_QUALITY:
            return "I couldn't get a good enough face image. Please try again."
        case ReasonCode.DENY_UNKNOWN_FACE:
            return "I don't recognize you yet. Please ask staff to enroll you."
        case ReasonCode.DENY_LOW_MATCH_CONFIDENCE:
            return "I couldn't verify your face confidently. Please try again."
        case ReasonCode.DENY_NO_ACTIVE_BOOKING:
            return "I can't find a booking for today. Please book in Slack."
        case ReasonCode.DENY_BOOKING_LOOKUP_FAILED:
            return "I can't verify bookings right now, so I need to keep the door locked."
        case ReasonCode.DENY_SALTO_ERROR:
            return "I verified your booking, but the door system failed. Please ask staff for help."
        case _:
            return "Something went wrong, so I need to keep the door locked."
