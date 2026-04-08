"""Async office bouncer state machine."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from app.config import Settings
from app.models.domain import BookingResult, FaceMatchResult, SystemStatusSnapshot
from app.models.enums import AppState, BookingStatus, Decision, HealthStatus, ReasonCode
from app.services.audio.barge_in_guard import BargeInGuard
from app.services.policy.decision_engine import DecisionEngine
from app.services.reachy.robot import RobotAdapter
from app.services.slack.schemas import BookingHelpRequest
from app.services.system.telemetry import TelemetryBroker


logger = logging.getLogger(__name__)


class OfficeBouncerStateMachine:
    """Coordinate vision, booking checks, robot UX, and door unlock."""

    def __init__(
        self,
        *,
        settings: Settings,
        decision_engine: DecisionEngine,
        identification_service,
        booking_client,
        unlock_service,
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
        self.unlock_service = unlock_service
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
        await self.transition(AppState.IDLE)
        await self.robot.set_idle_pose()

    async def transition(self, state: AppState) -> None:
        self.state = state
        self.telemetry.publish("state", {"state": state.value})

    async def handle_wake_word(self) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            access_event_id = str(uuid4())
            await self.transition(AppState.ENGAGED)
            await self.robot.animate_ack()
            await self._speak("Hi, one moment.")

            start = time.perf_counter()
            await self.transition(AppState.IDENTIFYING_FACE)
            face_match = await self.identification_service.identify()
            booking_result = BookingResult(status=BookingStatus.NONE, active=False)
            user = None

            if face_match.matched_user_id:
                user = self.user_repo.get(face_match.matched_user_id)

            if user is not None:
                await self.robot.look_at_face(face_match.bbox or (0, 0, 0, 0))
                await self.transition(AppState.CHECKING_BOOKING)
                booking_result = await self.booking_client.get_active_booking(
                    slack_user_id=user.slack_user_id,
                    now=datetime.now(timezone.utc),
                )

            decision = self.decision_engine.evaluate(
                face_match_result=face_match,
                booking_result=booking_result,
                system_status=self._current_system_status(),
            )

            unlock_result = None
            if decision.allowed and user is not None:
                await self.transition(AppState.UNLOCKING)
                unlock_result = await self.unlock_service.unlock_for_event(access_event_id=access_event_id)
                if not unlock_result.success:
                    decision = self.decision_engine.evaluate(
                        face_match_result=face_match,
                        booking_result=BookingResult(status=BookingStatus.LOOKUP_FAILED, active=False)
                        if unlock_result.detail.startswith("token_error")
                        else booking_result,
                        system_status=self._current_system_status(),
                    )
                    decision.reason_code = ReasonCode.DENY_SALTO_ERROR
                    decision.message = "I verified your booking, but the door system failed. Please ask staff for help."

            if decision.allowed and user is not None:
                greeting = user.slack_display_name or user.full_name
                await self._speak(f"Welcome, {greeting}. Opening the door.")
            else:
                await self.transition(AppState.DENIED)
                await self.robot.animate_deny()
                await self._speak(decision.message)
                if user is not None and decision.reason_code == ReasonCode.DENY_NO_ACTIVE_BOOKING:
                    await self.booking_client.send_booking_help(
                        BookingHelpRequest(
                            slack_user_id=user.slack_user_id,
                            reason_code=decision.reason_code.value,
                            access_event_id=access_event_id,
                        )
                    )
                self.guard.start_cooldown()

            latency_ms = int((time.perf_counter() - start) * 1000)
            event = self.access_events.create(
                face_match=face_match,
                booking_result=booking_result,
                decision=decision,
                unlock_result=unlock_result,
                latency_ms=latency_ms,
                metadata={
                    "state": self.state.value,
                    "spoken_message": self._last_spoken_message,
                    "booking_result": booking_result.metadata,
                    "unlock_result": unlock_result.metadata if unlock_result else None,
                },
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
            self.guard.end_engagement()
            if decision.allowed:
                await self.transition(AppState.IDLE)
            else:
                await self.transition(AppState.COOLDOWN)
                await asyncio.sleep(self.settings.cooldown_seconds)
                await self.transition(AppState.IDLE)

    async def handle_command(self, command) -> None:
        if command.value == "repeat" and self._last_spoken_message:
            await self._speak(self._last_spoken_message)
        elif command.value == "cancel":
            self.guard.end_engagement()
            await self.transition(AppState.COOLDOWN)
        elif command.value in {"help", "book now"}:
            await self._speak("Please book in Slack, then try again.")

    def _current_system_status(self) -> SystemStatusSnapshot:
        snapshot = self.system_health.snapshot()
        required = ["database", "booking", "salto", "camera"]
        ready = all(snapshot.get(component, HealthStatus.HEALTHY) != HealthStatus.ERROR for component in required)
        return SystemStatusSnapshot(ready=ready, components=snapshot)

    async def _speak(self, text: str) -> None:
        self._last_spoken_message = text
        await self.robot.speak(text)
