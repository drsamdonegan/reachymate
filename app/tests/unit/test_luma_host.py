from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin_api import router
from app.models.enums import CommandIntent
from app.runtime import AppContext
from app.services.live.session import build_session_start
from app.tests.unit.test_live_session import session_fixture, until


def host_context(settings):
    ctx = AppContext(settings=settings)
    settings.attended_chat_enabled = True
    session, wire, _, _ = session_fixture(settings)
    ctx.realtime = ctx.conversation.realtime = session
    session.on_closed = ctx.conversation.on_realtime_closed
    session.idle_timeout_paused = lambda: ctx.conversation.attended_chat_active
    session.tools.set_handler("robot_motion", ctx._robot_motion)
    ctx.event_checkin.event = {"id": "evt-demo", "name": "Demo"}
    return ctx, wire


def test_host_session_has_opt_in_vision_but_no_access_or_routing_tools(settings):
    settings.camera_vision_enabled = True
    config = build_session_start(settings, host_greeting="Welcome to our demo!")["session"]
    assert "inspect_camera" in config["instructions"]
    assert "Welcome to our demo!" in config["input"][0]["content"][0]["text"]
    tools = config["delegation"]["responses"]["tools"]
    assert {t["name"] for t in tools} == {
        "robot_motion",
        "inspect_camera",
        "wait_for_user",
        "end_conversation",
    }
    motion = next(t for t in tools if t["name"] == "robot_motion")["parameters"]["properties"]
    assert {"look", "pose", "gesture", "dance", "emotion", "recording", "list", "stop"} <= set(
        motion["action"]["enum"]
    )
    assert motion["duration_seconds"]["maximum"] == 20
    # Building this profile must not mutate the normal conversation's tools.
    ordinary = build_session_start(settings)["session"]["delegation"]["responses"]["tools"]
    assert "inspect_camera" in {t["name"] for t in ordinary}
    assert (
        "dance"
        in next(t for t in ordinary if t["name"] == "robot_motion")["parameters"]["properties"]["action"][
            "enum"
        ]
    )
    settings.camera_vision_enabled = False
    disabled = build_session_start(settings, host_greeting="Welcome")["session"]
    assert "inspect_camera" not in {t["name"] for t in disabled["delegation"]["responses"]["tools"]}


@pytest.mark.asyncio
async def test_host_keeps_luma_camera_and_supports_welcome_text_and_bounded_look(settings):
    ctx, wire = host_context(settings)
    ctx.robot.perform_motion = AsyncMock(return_value={"status": "started"})
    ctx.robot.speak = AsyncMock()
    try:
        with pytest.raises(ValueError, match="handoff"):
            await ctx.start_luma_host("Hello")
        await ctx.start_luma_express()
        await ctx.start_luma_host("Welcome to our demo!")
        assert ctx.realtime.running and ctx.audio_router._running
        assert ctx.conversation.attended_chat_active
        assert ctx.conversation.diagnostics()["camera_owner"] == "luma"
        assert not ctx.conversation.diagnostics()["dashboard_auto_start"]
        assert ctx.camera.get_frame() is None
        assert not ctx.attention.enabled and not ctx._model_vision_allowed()
        assert await ctx.conversation.request_access_check() == {"status": "unavailable"}
        await ctx.conversation.handle_command(CommandIntent.OPEN_DOOR)
        assert ctx.conversation._notice_task is None
        await ctx.welcome_luma_guest()
        ctx.robot.speak.assert_awaited_once_with("Welcome to our demo!")
        await ctx.send_voice_request("Look around")
        assert any(e.get("type") == "response.item.create" for e in wire.sent)
        for action in ("dance", "emotion", "recording", "pose", "gesture"):
            assert (await ctx._robot_motion({"action": action}))["status"] == "started"
        assert (
            await ctx._robot_motion(
                {"action": "look", "direction": "around", "duration_seconds": 20.0, "repeat": 3}
            )
        )["status"] == "started"
        request = ctx.robot.perform_motion.await_args.args[0]
        assert request.duration_seconds == 20 and request.repeat == 3
        await ctx.stop_luma_host()
        assert ctx.luma_express.active and ctx.luma_express.ready
        assert ctx.conversation.suspended and not ctx.conversation.event_host
        assert not ctx.realtime.running and not ctx.audio_router._running
        assert ctx.realtime.host_greeting is None
        assert ctx.camera.get_frame() is None
        await ctx.start_luma_host("A second welcome")
        await ctx.finish_luma_express(True)
        assert not ctx.realtime.running and not ctx.luma_express.active
        assert ctx.camera.get_frame() is not None
    finally:
        await ctx.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["expiry", "disconnect", "cancel", "audio_fault"])
async def test_host_cleanup_preserves_scanner_and_stops_microphone(settings, ending):
    ctx, _ = host_context(settings)
    restart = AsyncMock()
    ctx.audio_router.microphone.restart_streams = restart
    try:
        await ctx.start_luma_express()
        await ctx.start_luma_host("Welcome")
        if ending == "expiry":
            ctx.conversation._attended_until = time.monotonic() - 1
        elif ending == "disconnect":
            await ctx.realtime.stop(reason="test_disconnect")
        elif ending == "cancel":
            await ctx.conversation.handle_command(CommandIntent.CANCEL)
        else:
            with pytest.raises(asyncio.CancelledError):
                await ctx.audio_router._recover("AudioStalled")
        await until(lambda: not ctx.luma_express.host_voice)
        assert not ctx.realtime.running and not ctx.audio_router._running
        assert ctx.luma_express.active and ctx.camera.get_frame() is None
        assert ctx.conversation.suspended
        restart.assert_not_awaited()
    finally:
        await ctx.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_host_failed_connection_does_not_report_success_or_leave_mic_running(settings, cancelled):
    ctx, _ = host_context(settings)
    ctx.realtime.start = AsyncMock(
        side_effect=asyncio.CancelledError() if cancelled else ConnectionError("test")
    )
    try:
        await ctx.start_luma_express()
        with pytest.raises(asyncio.CancelledError if cancelled else ValueError):
            await ctx.start_luma_host("Welcome")
        assert ctx.luma_express.host_error
        assert not ctx.luma_express.host_voice and not ctx.audio_router._running
        assert ctx.conversation.suspended and ctx.camera.get_frame() is None
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_host_rejects_unadvertised_tool_even_if_backend_attempts_it(settings):
    session, wire, _, _ = session_fixture(settings)
    access = AsyncMock(return_value={"status": "unexpected"})
    session.tools.set_handler("request_access_check", access)
    session.host_greeting = "Welcome"
    try:
        await session.start()
        await session._execute_live_tool_batch(
            [{"call_id": "bad-tool", "name": "request_access_check", "arguments": "{}"}], session._generation
        )
        access.assert_not_awaited()
        outputs = [e["item"] for e in wire.sent if e.get("item", {}).get("type") == "function_call_output"]
        assert json.loads(outputs[0]["output"])["reason"] == "event_host_mode"
    finally:
        await session.stop()


def test_host_mutations_require_local_origin_and_bounded_greeting():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    for path, body in [("start", {"greeting": "Welcome"}), ("stop", None), ("welcome", None)]:
        assert (
            client.post(
                f"/api/admin/event-checkin/host/{path}",
                json=body,
                headers={"Origin": "https://untrusted.example"},
            ).status_code
            == 403
        )
    assert client.post("/api/admin/event-checkin/host/start", json={"greeting": "x" * 301}).status_code == 422
    assert (
        client.post(
            "/api/admin/robot/motions/reload", headers={"Origin": "https://untrusted.example"}
        ).status_code
        == 403
    )
