"""Async office bouncer state machine."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from app.config import Settings
from app.models.domain import (
    BookingResult,
    FaceMatchResult,
    FaceQuality,
    PolicyDecision,
    SystemStatusSnapshot,
)
from app.models.enums import (
    AppState,
    BookingStatus,
    Decision,
    FaceQualityFlag,
    ReasonCode,
)
from app.services.audio.barge_in_guard import BargeInGuard
from app.services.policy.decision_engine import DecisionEngine
from app.services.reachy.robot import RobotAdapter
from app.services.slack.schemas import BookingHelpRequest
from app.services.system.health import ACCESS_REQUIRED_COMPONENTS, components_ready
from app.services.system.telemetry import TelemetryBroker
from app.services.vision.embedding_guard import AccessAttemptScope

logger = logging.getLogger(__name__)


class OfficeBouncerStateMachine:
    """Coordinate vision, booking checks, robot UX, and the entry-action boundary."""

    def __init__(
        self,
        *,
        settings: Settings,
        decision_engine: DecisionEngine,
        identification_service,
        booking_client,
        door_action,
        access_events,
        system_health,
        user_repo,
        robot: RobotAdapter,
        telemetry: TelemetryBroker,
        guard: BargeInGuard,
    ) -> None:
        self.settings = settings
        self.decision_engine = decision_engine
        self.identification_service = identification_service
        self.booking_client = booking_client
        self.door_action = door_action
        self.access_events = access_events
        self.system_health = system_health
        self.user_repo = user_repo
        self.robot = robot
        self.telemetry = telemetry
        self.guard = guard
        self.state = AppState.BOOT
        self._lock = asyncio.Lock()
        self._last_spoken_message = ""

    async def boot(self) -> None:
        await self.transition(AppState.LISTENING_LOW_POWER)
        await self.robot.set_idle_pose()

    async def transition(self, state: AppState) -> None:
        self.state = state
        self.telemetry.publish("state", {"state": state.value})

    async def handle_wake_word(self, *, manage_guard: bool = True) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            access_event_id = str(uuid4())
            start = time.perf_counter()
            face_match = _unavailable_face_result()
            booking_result = BookingResult(status=BookingStatus.NONE, active=False)
            user = None
            unlock_result = None
            event_persisted = False
            cancellation: asyncio.CancelledError | None = None
            decision = PolicyDecision(
                decision=Decision.ERROR,
                reason_code=ReasonCode.ERROR_INTERNAL,
                allowed=False,
                message="Something went wrong, so I need to keep the door locked.",
            )

            try:
                # The final attempt ID is durable before camera, booking, or
                # actuator work. A process death from this point leaves an
                # explicit fail-closed record for recovery/reconciliation.
                self.access_events.create(
                    access_event_id=access_event_id,
                    face_match=face_match,
                    booking_result=booking_result,
                    decision=decision,
                    unlock_result=None,
                    latency_ms=0,
                    metadata={"phase": "attempt_started"},
                )
                event_persisted = True
                if not self._current_system_status().ready or getattr(self, "access_disabled", False):
                    raise AccessNotReady()
                await self.transition(AppState.ENGAGED)
                await self.robot.animate_ack()
                await self._speak("Hi, one moment.")

                await self.transition(AppState.IDENTIFYING_FACE)
                freeze = getattr(self.robot, "set_capture_freeze", None)
                if freeze:
                    value = freeze(True)
                    if inspect.isawaitable(value):
                        await value
                try:
                    with AccessAttemptScope(access_event_id, purpose="access"):
                        face_match = await asyncio.wait_for(
                            self.identification_service.identify(),
                            timeout=self.settings.face_identification_timeout_seconds,
                        )
                finally:
                    if freeze:
                        value = freeze(False)
                        if inspect.isawaitable(value):
                            await value

                if face_match.matched_user_id:
                    user = self.user_repo.get(face_match.matched_user_id)

                if user is not None and user.active and user.biometric_consent and not user.access_revoked:
                    await self.robot.look_at_face(face_match.bbox or (0, 0, 0, 0))
                    await self.transition(AppState.CHECKING_BOOKING)
                    booking_result = await asyncio.wait_for(
                        self.booking_client.get_active_booking(
                            slack_user_id=user.slack_user_id,
                            now=datetime.now(timezone.utc),
                        ),
                        timeout=self.settings.booking_lookup_timeout_seconds,
                    )

                if not self._current_system_status().ready or getattr(self, "access_disabled", False):
                    raise AccessNotReady()

                if user is not None and not user.active:
                    decision = PolicyDecision(
                        decision=Decision.DENY,
                        reason_code=ReasonCode.DENY_USER_INACTIVE,
                        allowed=False,
                        message="Your member access is inactive. Please ask staff for help.",
                    )
                elif user is not None and user.access_revoked:
                    decision = PolicyDecision(
                        decision=Decision.DENY,
                        reason_code=ReasonCode.DENY_ACCESS_REVOKED,
                        allowed=False,
                        message="Your door access has been revoked. Please ask staff for help.",
                    )
                elif user is not None and not user.biometric_consent:
                    decision = PolicyDecision(
                        decision=Decision.DENY,
                        reason_code=ReasonCode.DENY_CONSENT_REQUIRED,
                        allowed=False,
                        message="Biometric consent is not recorded, so I cannot use face access.",
                    )
                else:
                    decision = self.decision_engine.evaluate(
                        face_match_result=face_match,
                        booking_result=booking_result,
                        system_status=self._current_system_status(),
                        access_mode=self.settings.access_mode,
                    )

                if decision.reason_code == ReasonCode.SHADOW_WOULD_ALLOW:
                    unlock_result = await self.door_action.record_shadow(access_event_id=access_event_id)

                if decision.allowed and user is not None:
                    if not self._current_system_status().ready or getattr(self, "access_disabled", False):
                        raise AccessNotReady()
                    # Persist a fail-closed attempt before the actuator boundary. If the
                    # process dies after this write, the durable record remains ERROR.
                    pending_decision = PolicyDecision(
                        decision=Decision.ERROR,
                        reason_code=ReasonCode.ERROR_INTERNAL,
                        allowed=False,
                        message="Door actuation did not complete.",
                    )
                    self.access_events.update(
                        access_event_id=access_event_id,
                        face_match=face_match,
                        booking_result=booking_result,
                        decision=pending_decision,
                        unlock_result=None,
                        latency_ms=int((time.perf_counter() - start) * 1000),
                        metadata={"phase": "unlock_pending"},
                    )
                    await self.transition(AppState.UNLOCKING)
                    unlock_result = await asyncio.wait_for(
                        self.door_action.execute_for_event(access_event_id=access_event_id),
                        timeout=self.settings.door_action_timeout_seconds,
                    )
                    if not unlock_result.success:
                        decision = PolicyDecision(
                            decision=Decision.DENY,
                            reason_code=ReasonCode.DENY_DOOR_ACTION_ERROR,
                            allowed=False,
                            message=(
                                "I verified your booking, but the entry placeholder failed. "
                                "Please ask staff for help."
                            ),
                        )

                if decision.allowed and user is not None:
                    greeting = ""
                    if getattr(user, "greet_by_name", False):
                        greeting = (user.slack_display_name or user.full_name).split()[0]
                    if unlock_result and unlock_result.metadata.get("placeholder"):
                        await self._speak(
                            f"Welcome{', ' + greeting if greeting else ''}. The entry check passed. "
                            "Door control is only a placeholder, so no hardware command was sent."
                        )
                    else:
                        await self._speak("Please wait for staff to confirm entry.")
                else:
                    await self.transition(AppState.DENIED)
                    await self.robot.animate_deny()
                    shadow_privacy = self.settings.access_mode == "shadow" and not self.attended_demo_active
                    message = (
                        "Thanks, I've logged that. Please check in with the front desk."
                        if shadow_privacy
                        else decision.message
                    )
                    if decision.reason_code == ReasonCode.SHADOW_WOULD_ALLOW and self.attended_demo_active:
                        name = ""
                        if user is not None and getattr(user, "greet_by_name", False):
                            name = ", " + (user.slack_display_name or user.full_name).split()[0]
                        message = (
                            f"Welcome back{name}, you're booked in today. "
                            "I'm in demo mode, so the door isn't connected."
                        )
                    await self._speak(message)
                    if (
                        user is not None
                        and decision.reason_code == ReasonCode.DENY_NO_ACTIVE_BOOKING
                        and (self.settings.access_mode != "shadow" or self.attended_demo_active)
                    ):
                        await self.booking_client.send_booking_help(
                            BookingHelpRequest(
                                slack_user_id=user.slack_user_id,
                                reason_code=decision.reason_code.value,
                                access_event_id=access_event_id,
                            )
                        )
            except AccessNotReady:
                decision = PolicyDecision(
                    decision=Decision.ERROR,
                    reason_code=ReasonCode.ERROR_ACCESS_NOT_READY,
                    allowed=False,
                    message="Face check-in isn't available. Please see the front desk.",
                )
                await self._safe_transition(AppState.DENIED)
                await self._safe_deny_feedback(decision.message)
            except asyncio.CancelledError as exc:
                cancellation = exc
                decision = PolicyDecision(
                    decision=Decision.ERROR,
                    reason_code=ReasonCode.ERROR_INTERNAL,
                    allowed=False,
                    message="The access check was cancelled. The door remains locked.",
                )
                await self._safe_transition(AppState.ERROR)
            except Exception:
                logger.exception("Access attempt %s failed", access_event_id)
                decision = PolicyDecision(
                    decision=Decision.ERROR,
                    reason_code=ReasonCode.ERROR_INTERNAL,
                    allowed=False,
                    message="Something went wrong, so I need to keep the door locked.",
                )
                await self._safe_transition(AppState.ERROR)
                await self._safe_deny_feedback(decision.message)
            finally:
                if face_match.embedding is not None:
                    for index in range(len(face_match.embedding)):
                        face_match.embedding[index] = 0.0
                    face_match.embedding = None
                latency_ms = int((time.perf_counter() - start) * 1000)
                metadata = {
                    "state": self.state.value,
                    "speech_reason_code": decision.reason_code.value,
                    "booking_result": booking_result.metadata,
                    "unlock_result": unlock_result.metadata if unlock_result else None,
                }
                try:
                    if event_persisted:
                        event = self.access_events.update(
                            access_event_id=access_event_id,
                            face_match=face_match,
                            booking_result=booking_result,
                            decision=decision,
                            unlock_result=unlock_result,
                            latency_ms=latency_ms,
                            metadata=metadata,
                        )
                    else:
                        event = self.access_events.create(
                            access_event_id=access_event_id,
                            face_match=face_match,
                            booking_result=booking_result,
                            decision=decision,
                            unlock_result=unlock_result,
                            latency_ms=latency_ms,
                            metadata=metadata,
                        )
                    self.telemetry.publish(
                        "access_event",
                        {
                            "id": event.id,
                            "decision": event.decision,
                            "reason_code": event.reason_code,
                            "latency_ms": event.latency_ms,
                        },
                    )
                except Exception:
                    logger.exception("Failed to persist terminal access event %s", access_event_id)
                    self.telemetry.publish(
                        "access_event_persist_failed",
                        {
                            "id": access_event_id,
                            "decision": decision.decision.value,
                            "reason_code": decision.reason_code.value,
                        },
                    )

                if manage_guard:
                    self.guard.end_engagement()
                    if decision.allowed:
                        await self._safe_transition(AppState.LISTENING_LOW_POWER)
                    else:
                        self.guard.start_cooldown()
                        await self._safe_transition(AppState.COOLDOWN)
                        if cancellation is None and self.settings.cooldown_seconds > 0:
                            await asyncio.sleep(self.settings.cooldown_seconds)
                        await self._safe_transition(AppState.LISTENING_LOW_POWER)

            if cancellation is not None:
                raise cancellation

    async def handle_command(self, command) -> None:
        if command.value == "open door":
            await self._speak("I can start an access check, but a voice command cannot unlock the door.")
        elif command.value == "repeat" and self._last_spoken_message:
            await self._speak(self._last_spoken_message)
        elif command.value == "cancel":
            self.guard.end_engagement()
            await self.transition(AppState.COOLDOWN)
        elif command.value in {"help", "book now"}:
            await self._speak("Please book in Slack, then try again.")

    def _current_system_status(self) -> SystemStatusSnapshot:
        snapshot = self.system_health.snapshot()
        required = (
            tuple(c for c in ACCESS_REQUIRED_COMPONENTS if c != "face_liveness")
            if self.settings.access_mode == "shadow"
            else ACCESS_REQUIRED_COMPONENTS
        )
        ready = components_ready(snapshot, required)
        return SystemStatusSnapshot(
            ready=ready and not getattr(self, "access_disabled", False), components=snapshot
        )

    @property
    def attended_demo_active(self) -> bool:
        expires = self.settings.attended_demo_expires_at
        return self.settings.attended_demo and time.time() < expires <= time.time() + 14400

    async def _safe_transition(self, state: AppState) -> None:
        try:
            await self.transition(state)
        except Exception:
            logger.exception("Failed to transition to %s", state.value)

    async def _safe_deny_feedback(self, message: str) -> None:
        try:
            await self.robot.animate_deny()
        except Exception:
            logger.exception("Deny animation failed")
        try:
            await self._speak(message)
        except Exception:
            logger.exception("Deny speech failed")

    async def _speak(self, text: str) -> None:
        self._last_spoken_message = text
        await self.robot.speak(text)


def _unavailable_face_result() -> FaceMatchResult:
    return FaceMatchResult(
        matched_user_id=None,
        similarity=None,
        quality=FaceQuality(
            usable=False,
            flags=[FaceQualityFlag.NO_FACE],
            face_count=0,
        ),
    )


class AccessNotReady(Exception):
    """Stop an access attempt before any biometric capture."""
