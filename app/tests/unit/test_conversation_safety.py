from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.models.enums import CommandIntent
from app.services.audio.barge_in_guard import BargeInGuard
from app.services.conversation.coordinator import ConversationCoordinator
from app.services.reachy.speech import SpeechWorker
from app.services.system.telemetry import TelemetryBroker


@pytest.mark.asyncio
async def test_speech_failure_does_not_kill_queue() -> None:
    spoken = []

    async def speak(text):
        if text == "first":
            raise RuntimeError("sensitive error text")
        spoken.append(text)

    telemetry = TelemetryBroker()
    worker = SpeechWorker(speak_fn=speak, telemetry=telemetry)
    await worker.start()
    try:
        await worker.submit("first")
        await worker.submit("second")
        await asyncio.wait_for(worker.queue.join(), 1)
        assert spoken == ["second"]
        assert not worker.is_busy()
        assert any(e["payload"].get("event") == "speech_failed" for e in telemetry.snapshot())
        assert "sensitive error text" not in str(telemetry.snapshot())
    finally:
        await worker.stop()


class State:
    def __init__(self):
        self.captures = 0
        self.robot = SimpleNamespace(is_speaking=lambda: False)

    async def transition(self, state):
        return None

    async def handle_wake_word(self, **kwargs):
        self.captures += 1

    async def handle_command(self, command):
        return None


@pytest.mark.asyncio
async def test_attended_chat_survives_camera_loss_and_silence_but_expires(settings):
    settings.attended_chat_enabled = True
    guard = BargeInGuard(0)
    closed = []
    coordinator = ConversationCoordinator(
        settings=settings,
        guard=guard,
        state_machine=State(),
        realtime=None,
        system_health=SimpleNamespace(set_status=lambda *args: None),
        person_in_view=lambda: False,
    )
    coordinator.on_idle = lambda: closed.append(True)
    try:
        await coordinator.handle_wake(attended=True)
        coordinator._last_activity = time.monotonic() - 60
        coordinator._track_lost_at = time.monotonic() - 10
        await asyncio.sleep(0.3)
        assert coordinator.attended_chat_active and guard.is_engaged()
        assert not closed
        # Broader conversational audio is never biometric consent.
        assert (await coordinator.request_access_check())["status"] == "no_person_in_view"
        coordinator._attended_until = time.monotonic() - 1
        async with asyncio.timeout(2):
            while not closed:
                await asyncio.sleep(0.01)
        assert not coordinator.attended_chat_active and not guard.is_engaged()
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_normal_wake_keeps_face_departure_boundary_even_on_chat_bench(settings):
    settings.attended_chat_enabled = True
    closed = asyncio.Event()
    coordinator = ConversationCoordinator(
        settings=settings,
        guard=BargeInGuard(0),
        state_machine=State(),
        realtime=None,
        system_health=SimpleNamespace(set_status=lambda *args: None),
        on_idle=closed.set,
    )
    try:
        await coordinator.handle_wake()
        coordinator._track_lost_at = time.monotonic() - 10
        await asyncio.wait_for(closed.wait(), 2)
        assert not coordinator.attended_chat_active and not coordinator.guard.is_engaged()
    finally:
        await coordinator.stop()


@pytest.mark.parametrize("profile", ["production", "simulation", "test"])
def test_attended_chat_is_rejected_outside_staging(profile):
    from app.config import Settings

    with pytest.raises(ValueError, match="Attended chat requires"):
        Settings(_env_file=None, app_env=profile, attended_chat_enabled=True)


@pytest.mark.asyncio
async def test_operator_can_retry_failed_live_session_and_stop_without_waiting_for_idle(settings):
    from app.tests.unit.test_live_session import session_fixture

    settings.attended_chat_enabled = True
    session, wire, _, _ = session_fixture(settings)
    coordinator = ConversationCoordinator(
        settings=settings,
        guard=BargeInGuard(0),
        state_machine=State(),
        realtime=session,
        system_health=SimpleNamespace(set_status=lambda *args: None),
    )
    session.on_closed = coordinator.on_realtime_closed
    session.idle_timeout_paused = lambda: coordinator.attended_chat_active
    try:
        await coordinator.handle_wake(attended=True)
        await session.stop(reason="connection_error")
        assert coordinator._local_only_until > time.monotonic()
        await coordinator.handle_wake(attended=True)
        assert session.running and coordinator.attended_chat_active
        assert sum(e["type"] == "session.start" for e in wire.sent) == 2
        await coordinator.handle_command(CommandIntent.CANCEL)
        assert not session.running and not coordinator.guard.is_engaged()
        assert not coordinator.attended_chat_active
        assert coordinator._heartbeat_task.done()
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_local_wake_never_captures(settings) -> None:
    state = State()
    coordinator = ConversationCoordinator(
        settings=settings,
        guard=BargeInGuard(0),
        state_machine=state,
        realtime=None,
        system_health=SimpleNamespace(set_status=lambda *args: None),
    )
    await coordinator.handle_wake()
    assert state.captures == 0
    await coordinator.stop()


@pytest.mark.asyncio
async def test_capture_needs_exact_local_confirmation_after_notice(settings) -> None:
    state = State()
    notice_started = asyncio.Event()
    notice_finish = asyncio.Event()

    async def notice():
        notice_started.set()
        await notice_finish.wait()

    coordinator = ConversationCoordinator(
        settings=settings,
        guard=BargeInGuard(0),
        state_machine=state,
        realtime=None,
        system_health=SimpleNamespace(set_status=lambda *args: None),
        play_notice=notice,
        person_in_view=lambda: True,
    )
    await coordinator.handle_wake()
    await coordinator.handle_command(CommandIntent.OPEN_DOOR)
    await notice_started.wait()
    await coordinator.handle_command(CommandIntent.CONSENT_CONFIRM)
    assert state.captures == 0
    notice_finish.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await coordinator.handle_command(CommandIntent.CONSENT_CONFIRM)
    await asyncio.sleep(0)
    assert state.captures == 1
    await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["silent_robot_false", "person_left", "expired", "decline"])
async def test_confirmation_fails_closed_when_consent_conditions_change(settings, condition):
    state = State()
    in_view = True
    speaking = False

    async def notice():
        return None

    coordinator = ConversationCoordinator(
        settings=settings,
        guard=BargeInGuard(0),
        state_machine=state,
        realtime=None,
        system_health=SimpleNamespace(set_status=lambda *args: None),
        play_notice=notice,
        person_in_view=lambda: in_view,
        robot_speaking=lambda: speaking,
    )
    await coordinator.handle_wake()
    await coordinator.handle_command(CommandIntent.OPEN_DOOR)
    await asyncio.sleep(0)
    assert coordinator._consent_deadline is not None
    command = CommandIntent.CONSENT_CONFIRM
    if condition == "silent_robot_false":
        speaking = True
    elif condition == "person_left":
        in_view = False
    elif condition == "expired":
        coordinator._consent_deadline = 0
    elif condition == "decline":
        command = CommandIntent.CONSENT_DECLINE
    await coordinator.handle_command(command)
    await asyncio.sleep(0)
    assert state.captures == 0
    await coordinator.stop()


@pytest.mark.asyncio
async def test_silence_audio_does_not_refresh_engagement(settings):
    state = State()
    coordinator = ConversationCoordinator(
        settings=settings,
        guard=BargeInGuard(0),
        state_machine=state,
        realtime=None,
        system_health=SimpleNamespace(set_status=lambda *args: None),
    )
    await coordinator.handle_wake()
    before = coordinator._last_activity
    coordinator.feed_audio(b"\0\0" * 1280)
    assert coordinator._last_activity == before
    coordinator.feed_audio(b"\0\0" * 1280, speech_active=True)
    assert coordinator._last_activity >= before
    await coordinator.stop()


@pytest.mark.asyncio
async def test_cancel_cancels_inflight_capture_before_stopping_cloud(settings):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    state = State()

    async def capture(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    state.handle_wake_word = capture
    coordinator = ConversationCoordinator(
        settings=settings,
        guard=BargeInGuard(0),
        state_machine=state,
        realtime=None,
        system_health=SimpleNamespace(set_status=lambda *args: None),
        play_notice=lambda: asyncio.sleep(0),
    )
    await coordinator.handle_wake()
    await coordinator.handle_command(CommandIntent.OPEN_DOOR)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await coordinator.handle_command(CommandIntent.CONSENT_CONFIRM)
    await entered.wait()
    await coordinator.handle_command(CommandIntent.CANCEL)
    assert cancelled.is_set()
    assert not coordinator.guard.is_engaged()
    await coordinator.stop()


@pytest.mark.asyncio
async def test_routed_guest_cannot_request_or_confirm_face_capture(settings):
    state = State()
    notices = []

    async def notice():
        notices.append("played")

    coordinator = ConversationCoordinator(
        settings=settings,
        guard=BargeInGuard(0),
        state_machine=state,
        realtime=None,
        system_health=SimpleNamespace(set_status=lambda *args: None),
        play_notice=notice,
    )
    await coordinator.handle_wake()
    await coordinator.mark_routed()
    assert (await coordinator.request_access_check())["status"] == "visitor_routed"
    await coordinator.handle_command(CommandIntent.OPEN_DOOR)
    await coordinator.handle_command(CommandIntent.CONSENT_CONFIRM)
    await asyncio.sleep(0)
    assert state.captures == 0
    assert notices == []
    await coordinator.stop()


@pytest.mark.asyncio
async def test_routing_cancels_pending_notice_and_clears_consent(settings):
    state = State()
    started = asyncio.Event()

    async def notice():
        started.set()
        await asyncio.Event().wait()

    coordinator = ConversationCoordinator(
        settings=settings,
        guard=BargeInGuard(0),
        state_machine=state,
        realtime=None,
        system_health=SimpleNamespace(set_status=lambda *args: None),
        play_notice=notice,
    )
    await coordinator.handle_wake()
    await coordinator.request_access_check()
    await started.wait()
    await coordinator.mark_routed()
    await coordinator.handle_command(CommandIntent.CONSENT_CONFIRM)
    assert coordinator._notice_task.done()
    assert coordinator._consent_deadline is None
    assert state.captures == 0
    await coordinator.stop()
