from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin_api import router
from app.models.enums import AppState, CommandIntent
from app.services.live.session import build_session_start
from app.services.reachy.motion.manager import MotionManager, MotionMode
from app.services.vision.attention import AttentionTracker, CameraCalibration
from app.services.vision.greeter import ArrivalGate, EventGreeter, visual_target
from app.tests.unit.test_live_session import until
from app.tests.unit.test_luma_host import host_context


def test_arrival_gate_requires_stable_presence_and_departure_before_repeat():
    gate = ArrivalGate()
    assert not gate.update(True, 0)
    assert gate.update(True, 0.9)
    gate.mark_welcomed(0.9)
    assert not gate.update(True, 60)  # The same arrival never gets a timed repeat.
    assert not gate.update(False, 62)
    assert not gate.update(True, 63)  # Brief occlusion does not re-arm.
    assert not gate.update(False, 68.1)
    assert not gate.update(True, 69)
    assert gate.update(True, 70)
    gate.mark_welcomed(70)
    gate.update(False, 76)
    gate.update(True, 77)
    assert not gate.update(True, 78)  # At least fifteen seconds between welcomes.
    assert gate.update(True, 86)


def test_visible_face_outside_tracking_zone_does_not_repeat_welcome():
    gate = ArrivalGate()
    gate.update(True, 0)
    gate.mark_welcomed(1)
    for now in range(2, 60):
        assert not gate.update(False, now, visible=True)
    gate.update(True, 60)
    assert not gate.update(True, 61)
    gate.update(False, 67)
    gate.update(True, 68)
    assert gate.update(True, 69)


@pytest.mark.asyncio
async def test_face_outside_tracking_zone_preserves_welcome_latch_but_allows_search():
    follow = AsyncMock()
    greeter = EventGreeter(
        observation=lambda: None,
        follow=follow,
        welcome=AsyncMock(),
        visible=lambda: True,
    )
    await greeter.start("Welcome")
    try:
        greeter.gate.mark_welcomed(time.monotonic() - 60)
        greeter.last_target_at = time.monotonic() - 4
        await until(lambda: follow.await_count > 0)
        assert follow.call_args.args[0] is None
        assert greeter.gate.welcomed
        greeter.welcome.assert_not_awaited()
    finally:
        await greeter.stop()


@pytest.mark.asyncio
async def test_slow_greeting_never_blocks_face_following_and_stop_cancels_it():
    entered, finished = asyncio.Event(), asyncio.Event()
    follow = AsyncMock()

    async def welcome(_text):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    greeter = EventGreeter(
        observation=lambda: SimpleNamespace(in_zone=True, timestamp=time.monotonic(), bbox=(1, 2, 30, 40)),
        follow=follow,
        welcome=welcome,
    )
    await greeter.start("Welcome")
    greeter.gate.stable_seconds = 0
    await asyncio.wait_for(entered.wait(), 2)
    before = follow.await_count
    await asyncio.sleep(0.35)
    assert follow.await_count > before
    await greeter.stop()
    assert finished.is_set() and not greeter.enabled
    before = follow.await_count
    await asyncio.sleep(0.2)
    assert follow.await_count == before


def test_pixel_servo_has_correct_signs_bounds_and_stop_priority():
    pytest.importorskip("reachy_mini")
    from reachy_mini.utils import create_head_pose

    right_below = visual_target(np.eye(4), (1500, 800, 100, 100), 1920, 1080, 0)
    assert math.atan2(right_below[1, 0], right_below[0, 0]) < 0
    assert math.asin(-right_below[2, 0]) > 0
    bounded = visual_target(create_head_pose(yaw=35, pitch=-18), (0, 0, 100, 100), 1920, 1080, 0)
    assert math.degrees(math.atan2(bounded[1, 0], bounded[0, 0])) <= 35.001
    assert math.degrees(math.asin(-bounded[2, 0])) >= -18.001
    for elapsed in range(50):
        scan = visual_target(np.eye(4), None, 1920, 1080, elapsed)
        assert abs(math.degrees(math.atan2(scan[1, 0], scan[0, 0]))) <= 25.001
    manager = MotionManager(SimpleNamespace())
    manager.set_mode(MotionMode.ENGAGED)
    assert manager.set_tracking_target(right_below, autonomous=True)
    manager.stop_expression()
    assert not manager.set_tracking_target(right_below, autonomous=True)
    manager.set_mode(MotionMode.CAPTURE_FREEZE)
    assert not manager.set_tracking_target(right_below, autonomous=True)
    manager.set_mode(MotionMode.PARKED)
    assert not manager.set_tracking_target(right_below, autonomous=True)


def test_greeter_voice_starts_silent_with_native_vision_and_no_access_tools(settings):
    settings.camera_vision_enabled = True
    config = build_session_start(settings, greeter_greeting="Welcome to our demo!")["session"]
    assert "Stay silent at startup" in config["instructions"]
    assert "Luma" not in config["instructions"]
    assert "Welcome to our demo!" in config["input"][0]["content"][0]["text"]
    assert {t["name"] for t in config["delegation"]["responses"]["tools"]} == {
        "robot_motion",
        "inspect_camera",
        "wait_for_user",
        "end_conversation",
    }


@pytest.mark.asyncio
async def test_greeter_runtime_rearms_between_cloud_sessions_without_releasing_camera(settings):
    settings.camera_vision_enabled = True
    ctx, _ = host_context(settings)
    ctx.attention.tracker = AttentionTracker(
        CameraCalibration(np.array([[500, 0, 320], [0, 500, 240], [0, 0, 1]]), np.zeros(5), 640, 480)
    )
    ctx.robot.speak = AsyncMock()
    present = False
    ctx.greeter.observation = lambda: (
        SimpleNamespace(in_zone=True, timestamp=time.monotonic(), bbox=(200, 100, 100, 100))
        if present
        else None
    )
    try:
        result = await ctx.start_event_greeter("Welcome to the demo!")
        assert result["enabled"] and ctx.realtime.running and ctx.attention.enabled
        assert ctx.conversation.diagnostics()["camera_owner"] == "reachy"
        assert ctx.conversation.diagnostics()["mode"] == "event_greeter"
        assert ctx.camera.get_frame() is not None
        assert not ctx.attention.auto_tracking_enabled
        assert not ctx._event_camera_allowed()
        assert await ctx.conversation.request_access_check() == {"status": "unavailable"}
        await ctx.conversation.handle_command(CommandIntent.OPEN_DOOR)
        assert ctx.conversation._notice_task is None
        with pytest.raises(ValueError, match="greeter"):
            await ctx.start_luma_express()
        ctx.greeter.gate.stable_seconds = 0
        present = True
        await until(lambda: ctx.greeter.welcomes == 1)
        ctx.robot.speak.assert_awaited_once_with("Welcome to the demo!")
        await ctx.conversation._end_visitor(reason="attended_chat_expired")
        assert ctx.greeter.enabled and ctx.attention.enabled and not ctx.realtime.running
        await ctx._park_after_idle(delay=0)
        assert ctx.state_machine.state != AppState.DORMANT
        await asyncio.sleep(0.2)
        assert ctx.greeter.welcomes == 1 and not ctx.realtime.running
        ctx.greeter.gate.welcomed = False
        ctx.greeter.gate.last_welcome = -float("inf")
        await until(lambda: ctx.greeter.welcomes == 2)
        assert ctx.realtime.running
        await ctx.stop_voice_test()
        assert not ctx.greeter.enabled and not ctx.attention.enabled and not ctx.realtime.running
        assert not ctx.conversation.event_greeter
    finally:
        await ctx.stop()


def test_greeter_controls_reject_foreign_origins():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    for action in ("start", "stop"):
        assert (
            client.post(
                f"/api/admin/event-greeter/{action}", headers={"Origin": "https://wrong.example"}
            ).status_code
            == 403
        )
