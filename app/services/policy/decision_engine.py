"""Decision engine."""

from __future__ import annotations

from app.models.domain import BookingResult, FaceMatchResult, PolicyDecision, SystemStatusSnapshot
from app.models.enums import Decision, ReasonCode
from app.services.policy.rules import booking_reason, face_reason, system_can_authorize
from app.services.system.health import ACCESS_REQUIRED_COMPONENTS, components_ready


class DecisionEngine:
    """Evaluate the allow/deny policy."""

    def __init__(self, *, face_match_threshold: float, access_mode: str = "enforce") -> None:
        self.face_match_threshold = face_match_threshold
        if access_mode not in {"shadow", "enforce"}:
            raise ValueError("access_mode must be shadow or enforce")
        self.access_mode = access_mode

    def evaluate(
        self,
        *,
        face_match_result: FaceMatchResult,
        booking_result: BookingResult,
        system_status: SystemStatusSnapshot,
        access_mode: str | None = None,
    ) -> PolicyDecision:
        """Return the final policy decision."""

        mode = access_mode or self.access_mode
        if mode not in {"shadow", "enforce"}:
            raise ValueError("access_mode must be shadow or enforce")
        if mode == "shadow":
            ready = components_ready(
                system_status.components,
                [name for name in ACCESS_REQUIRED_COMPONENTS if name != "face_liveness"],
            )
        else:
            ready = system_can_authorize(system_status) and components_ready(
                system_status.components, ACCESS_REQUIRED_COMPONENTS
            )
        if not ready:
            return PolicyDecision(
                decision=Decision.ERROR,
                reason_code=ReasonCode.ERROR_ACCESS_NOT_READY,
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

        if mode == "shadow":
            return PolicyDecision(
                decision=Decision.DENY,
                reason_code=ReasonCode.SHADOW_WOULD_ALLOW,
                allowed=False,
                message="Thanks, I've logged that. Please check in with the front desk.",
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
        case ReasonCode.DENY_USER_INACTIVE:
            return "Your member access is inactive. Please ask staff for help."
        case ReasonCode.DENY_CONSENT_REQUIRED:
            return "Biometric consent is not recorded, so I cannot use face access."
        case ReasonCode.DENY_ACCESS_REVOKED:
            return "Your door access has been revoked. Please ask staff for help."
        case ReasonCode.DENY_NO_ACTIVE_BOOKING:
            return "I can't find a booking for today. Please book in Slack."
        case ReasonCode.DENY_BOOKING_LOOKUP_FAILED:
            return "I can't verify bookings right now, so I need to keep the door locked."
        case ReasonCode.DENY_SALTO_ERROR:
            return "I verified your booking, but the door system failed. Please ask staff for help."
        case _:
            return "Something went wrong, so I need to keep the door locked."
