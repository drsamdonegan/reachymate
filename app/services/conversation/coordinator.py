"""Own visitor engagement; only post-notice local consent starts biometrics."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from app.config import Settings
from app.models.enums import AppState, CommandIntent, HealthStatus
from app.services.audio.barge_in_guard import BargeInGuard

if TYPE_CHECKING:
    from app.services.realtime import OpenAIRealtimeSession

logger = logging.getLogger(__name__)


class ConversationCoordinator:
    """A wake is conversational attention, never biometric consent."""

    def __init__(
        self,
        *,
        settings: Settings,
        guard: BargeInGuard,
        state_machine,
        realtime: OpenAIRealtimeSession | None,
        system_health,
        play_notice: Callable[[], Any] | None = None,
        person_in_view: Callable[[], bool] | None = None,
        robot_speaking: Callable[[], bool] | None = None,
        prepare_engagement: Callable[[], Any] | None = None,
        reset_local_grammar: Callable[[], Any] | None = None,
        on_idle: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.guard = guard
        self.state_machine = state_machine
        self.realtime = realtime
        self.system_health = system_health
        self.play_notice = play_notice
        self.person_in_view = person_in_view or (lambda: settings.reachy_simulation)
        self.robot_speaking = robot_speaking or getattr(
            getattr(state_machine, "robot", None), "is_speaking", lambda: False
        )
        self.prepare_engagement = prepare_engagement
        self.reset_local_grammar = reset_local_grammar
        self.on_idle = on_idle
        self.session_ref = str(uuid4())
        self._access_task: asyncio.Task[None] | None = None
        self._notice_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._ending_task: asyncio.Task[None] | None = None
        self._consent_deadline: float | None = None
        self._consent_requested = False
        self._routed = False
        self._engaged = False
        self._ending = False
        self._last_activity = 0.0
        self._local_only_until = 0.0
        self._wake_lock = asyncio.Lock()
        self.suspended = False
        self.event_host = False
        self.host_connected = False
        self._track_ref: str | None = None
        self._pending_track_ref: str | None = None
        self._track_changed_at: float | None = None
        self._track_lost_at: float | None = None
        self._attended_until = 0.0
        if realtime is not None:
            realtime.on_activity = self.gated_activity
            realtime.tools.set_handler("request_access_check", self._tool_access_request)
            realtime.tools.set_handler("end_conversation", self._tool_end_conversation)

    @property
    def attended_chat_active(self) -> bool:
        return self._engaged and time.monotonic() < self._attended_until

    def diagnostics(self) -> dict[str, Any]:
        return {
            "attended_available": self.settings.attended_chat_enabled,
            "dashboard_auto_start": self.settings.dashboard_auto_chat
            and not self.suspended
            and not self.event_host,
            "pause_reason": "luma_express" if self.suspended else None,
            "camera_owner": "luma" if self.suspended or self.event_host else "reachy",
            "max_seconds": self.settings.attended_chat_max_seconds,
            "mode": "event_host"
            if self.event_host
            else "attended_chat"
            if self.attended_chat_active
            else "concierge",
            "remaining_seconds": round(max(0, self._attended_until - time.monotonic()))
            if self.attended_chat_active
            else 0,
        }

    async def handle_wake(self, *, attended: bool = False) -> None:
        async with self._wake_lock:
            if self.suspended or (self.event_host and not attended):
                return
            if getattr(self.state_machine, "state", None) == AppState.SAFE_HOLD:
                return
            if attended and not self.settings.attended_chat_enabled:
                raise RuntimeError("Attended chat is unavailable in this profile")
            if self._engaged:
                if not attended or self.realtime is None or self.realtime.running:
                    if attended:
                        self._attended_until = time.monotonic() + self.settings.attended_chat_max_seconds
                        self._track_lost_at = self._track_changed_at = None
                    self.gated_activity()
                    return
                await self._end_visitor(reason="operator_restart")
            self._attended_until = (
                time.monotonic() + self.settings.attended_chat_max_seconds if attended else 0.0
            )
            self.session_ref = str(uuid4())
            self._consent_requested = False
            self._routed = False
            self._consent_deadline = None
            self._track_ref = self._pending_track_ref = None
            self._track_changed_at = self._track_lost_at = None
            self._ending = False
            self._local_only_until = 0.0
            self._engaged = True
            self.gated_activity()
            await self.state_machine.transition(AppState.ENGAGED)
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="engagement-heartbeat")
            try:
                tasks = []
                if self.prepare_engagement is not None:
                    tasks.append(self._invoke(self.prepare_engagement))
                if self.realtime is not None:
                    tasks.append(self.realtime.start())
                if tasks:
                    await asyncio.gather(*tasks)
                if self.realtime is not None:
                    self.system_health.set_status(
                        "realtime", HealthStatus.HEALTHY, {"model": self.settings.voice_model}
                    )
                    await self.realtime.greet()
                else:
                    self._local_only_until = time.monotonic() + 20
            except Exception as exc:
                logger.warning("OpenAI voice engagement failed: %s", type(exc).__name__)
                self.system_health.set_status("realtime", HealthStatus.ERROR, {"error": type(exc).__name__})
                self._local_only_until = time.monotonic() + 20
                self.gated_activity()
                # The local grammar remains available during cloud failure.
                await self.state_machine.transition(AppState.ENGAGED)

    def gated_activity(self) -> None:
        """Called only for gated visitor speech, output audio and accepted tool calls."""

        self._last_activity = time.monotonic()
        if self._engaged:
            self.guard.begin_engagement(
                max(2, int(getattr(self.settings, "engagement_silence_seconds", 20)) + 2)
            )

    def update_engaged_track(self, track_ref: str | None) -> None:
        """Close visitor context when the attention tracker loses or replaces them."""

        if self.attended_chat_active:
            return
        now = time.monotonic()
        if track_ref is None:
            if self._track_ref is not None and self._track_lost_at is None:
                self._track_lost_at = now
            return
        self._track_lost_at = None
        if self._track_ref is None:
            self._track_ref = track_ref
        elif track_ref != self._track_ref:
            if track_ref != self._pending_track_ref:
                self._pending_track_ref = track_ref
                self._track_changed_at = now
        else:
            self._pending_track_ref = None
            self._track_changed_at = None

    def feed_audio(self, chunk: bytes, *, speech_active: bool = False) -> bool:
        if speech_active:
            self.gated_activity()
            if self.realtime is not None:
                self.realtime.touch_activity()
        if self.realtime is None:
            return False
        return self.realtime.feed_audio(chunk)

    async def request_access_check(self) -> dict[str, Any]:
        if self.suspended or self.event_host:
            return {"status": "unavailable"}
        if not self._engaged:
            return {"status": "unavailable"}
        if self._routed:
            return {"status": "visitor_routed"}
        if not self.person_in_view():
            return {"status": "no_person_in_view"}
        if self._consent_requested or (self._access_task is not None and not self._access_task.done()):
            return {"status": "already_requested"}
        if self.play_notice is None:
            self.system_health.set_status(
                "consent_notice", HealthStatus.ERROR, {"error": "fixed_notice_unavailable"}
            )
            return {"status": "unavailable"}
        self._consent_requested = True
        self._notice_task = asyncio.create_task(self._play_consent_notice(), name="consent-notice")
        return {
            "status": "consent_prompted",
            "say_hint": "The local biometric notice will play. Wait for the visitor's own response.",
        }

    async def handle_command(self, command: CommandIntent) -> None:
        if self.suspended:
            return
        if command == CommandIntent.CANCEL:
            await self._end_visitor(reason="visitor_cancelled")
            return
        if self.event_host:
            return
        if not self._engaged:
            return
        if command in {CommandIntent.OPEN_DOOR, CommandIntent.CONSENT_CONFIRM}:
            if self._routed:
                return
            if command == CommandIntent.CONSENT_CONFIRM and self._consent_deadline is not None:
                valid = (
                    time.monotonic() <= self._consent_deadline
                    and self.person_in_view()
                    and not self.robot_speaking()
                    and self.guard.is_engaged()
                )
                self._consent_deadline = None
                if valid:
                    if self._access_task is None or self._access_task.done():
                        self._access_task = asyncio.create_task(
                            self._run_access_check(), name="local-access-check"
                        )
                else:
                    await self._finish_consent()
                return
            await self.request_access_check()
            return
        if self._consent_deadline is not None:
            # An unrelated utterance, decline or silence never authorizes capture.
            self._consent_deadline = None
            await self._finish_consent()
        if command == CommandIntent.CONSENT_DECLINE:
            return
        if self.realtime is None:
            await self.state_machine.handle_command(command)

    async def stop(self) -> None:
        self._ending = True
        self._engaged = False
        self._attended_until = 0.0
        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in (self._access_task, self._notice_task, self._heartbeat_task, self._ending_task)
            if task is not None and task is not current
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._access_task = self._notice_task = self._heartbeat_task = self._ending_task = None
        self._consent_deadline = None
        self.guard.end_engagement()
        if self.realtime is not None:
            await self.realtime.stop(reason="runtime_shutdown")

    async def suspend(self) -> None:
        self.suspended = True
        # Finish a wake already in progress, then stop it; later wakes cannot reconnect.
        async with self._wake_lock:
            await self.stop()

    async def mark_routed(self) -> None:
        """Routing commits this visitor to the path with no biometric capture."""

        self._routed = True
        self._consent_deadline = None
        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in (self._notice_task, self._access_task)
            if task is not None and task is not current and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._finish_consent()

    async def on_realtime_closed(self) -> None:
        self._attended_until = 0.0
        if self._ending or not self._engaged:
            self.guard.end_engagement()
            return
        self._local_only_until = time.monotonic() + 20
        self.gated_activity()
        if self._access_task is None or self._access_task.done():
            await self.state_machine.transition(AppState.ENGAGED)

    async def _play_consent_notice(self) -> None:
        try:
            self._consent_deadline = None
            await self.state_machine.transition(AppState.CONSENT_PROMPT)
            if self.realtime is not None and self.realtime.running:
                await self.realtime.begin_local_playback()
            await self._invoke(self.play_notice)
            if not self._engaged or self._ending:
                return
            if self.reset_local_grammar is not None:
                await self._invoke(self.reset_local_grammar)
            self._consent_deadline = time.monotonic() + getattr(self.settings, "consent_timeout_seconds", 8.0)
            self.gated_activity()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._consent_deadline = None
            self.system_health.set_status("consent_notice", HealthStatus.ERROR, {"error": type(exc).__name__})
            await self._finish_consent()
        finally:
            if self.realtime is not None:
                self.realtime.end_local_playback()

    async def _finish_consent(self) -> None:
        if self.realtime is not None:
            self.realtime.resume_input()
        if self._engaged and not self._ending:
            await self.state_machine.transition(AppState.ENGAGED)

    async def _run_access_check(self) -> None:
        try:
            # This is the only capture entry. Model tools and wake cannot reach it.
            await self.state_machine.handle_wake_word(manage_guard=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Access check failed: %s", type(exc).__name__)
        finally:
            if self._engaged and not self._ending:
                self.gated_activity()
                await self._finish_consent()

    async def _heartbeat(self) -> None:
        while self._engaged:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            if (
                self.event_host
                and self.host_connected
                and self.realtime is not None
                and not self.realtime.running
            ):
                await self._end_visitor(reason="event_host_disconnected")
                return
            if self._attended_until and now >= self._attended_until:
                await self._end_visitor(reason="attended_chat_expired")
                return
            if not self.attended_chat_active and (
                (self._track_changed_at is not None and now - self._track_changed_at >= 1.5)
                or (self._track_lost_at is not None and now - self._track_lost_at >= 8)
            ):
                await self._end_visitor(reason="visitor_left")
                return
            if self._consent_deadline is not None and now > self._consent_deadline:
                self._consent_deadline = None
                await self._finish_consent()
            if self._access_task is not None and not self._access_task.done():
                self.guard.begin_engagement(2)
                continue
            if self._notice_task is not None and not self._notice_task.done():
                self.guard.begin_engagement(2)
                continue
            if (
                self.realtime is not None
                and not self.realtime.running
                and self._local_only_until
                and now >= self._local_only_until
            ):
                await self._end_visitor(reason="local_grammar_window_expired")
                return
            limit = getattr(self.settings, "engagement_silence_seconds", 20.0)
            if self.attended_chat_active:
                self.guard.begin_engagement(2)
            elif now - self._last_activity >= limit:
                await self._end_visitor(reason="visitor_silent")
                return

    async def _end_visitor(self, *, reason: str) -> None:
        self._ending = True
        self._engaged = False
        self._attended_until = 0.0
        self._consent_deadline = None
        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in (self._access_task, self._notice_task, self._heartbeat_task)
            if task is not None and task is not current
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.realtime is not None:
            try:
                if self.realtime.running and reason == "visitor_silent":
                    await asyncio.wait_for(
                        self.realtime.announce("Goodbye. Say Hey Reachy if you need a hand."), 3.0
                    )
            except Exception:
                pass
            await self.realtime.stop(reason=reason)
        robot = getattr(self.state_machine, "robot", None)
        speech = getattr(robot, "speech", None)
        if speech is not None:
            speech.drop_pending()
        self.guard.end_engagement()
        self.guard.start_cooldown()
        await self.state_machine.transition(AppState.COOLDOWN)
        self._track_ref = self._pending_track_ref = None
        self._track_changed_at = self._track_lost_at = None
        if self.on_idle is not None:
            await self._invoke(self.on_idle)

    async def _tool_access_request(self, _arguments: dict[str, Any], _call_id: str) -> dict[str, Any]:
        return await self.request_access_check()

    async def _tool_end_conversation(self, _arguments: dict[str, Any], _call_id: str) -> dict[str, Any]:
        async def finish() -> None:
            if self.realtime is not None:
                await self.realtime.wait_response_finished()
            await self._end_visitor(reason="visitor_done")

        if self._ending_task is None or self._ending_task.done():
            self._ending_task = asyncio.create_task(finish(), name="end-conversation")
        return {"status": "ending"}

    @staticmethod
    async def _invoke(callback: Callable[[], Any] | None) -> None:
        if callback is None:
            return
        result = callback()
        if inspect.isawaitable(result):
            await result
