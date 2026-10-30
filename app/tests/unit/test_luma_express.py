from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin_api import router
from app.models.enums import AppState, CommandIntent, RuntimeMode
from app.runtime import AppContext
from app.scripts import launch_reachy
from app.services.events.express import LumaExpressHandoff
from app.services.vision.camera import ReachyCameraAdapter


def test_handoff_survives_restart_and_only_explicit_finish_clears_it(tmp_path):
    path = tmp_path / "handoff.json"
    handoff = LumaExpressHandoff(path)
    handoff.begin("evt-demo")
    assert path.stat().st_mode & 0o777 == 0o600
    restored = LumaExpressHandoff(path)
    assert restored.active and not restored.ready
    assert restored.snapshot()["url"] == "https://luma.com/check-in/evt-demo/scan"
    with pytest.raises(ValueError, match="before changing events"):
        restored.begin("evt-other")
    restored.finish()
    assert not LumaExpressHandoff(path).active
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("content", ["corrupt", "null", "{}", '{"event_id": 42}', "x" * 1025])
def test_corrupt_marker_keeps_camera_and_voice_protected(tmp_path, content):
    path = tmp_path / "handoff.json"
    path.write_text(content)
    handoff = LumaExpressHandoff(path)
    assert handoff.active and handoff.snapshot()["url"] is None
    with pytest.raises(ValueError):
        handoff.begin("evt-demo")
    handoff.finish()
    assert not path.exists()


@pytest.mark.asyncio
async def test_handoff_blocks_all_local_camera_and_voice_paths_until_return(settings):
    ctx = AppContext(settings=settings)
    ctx.event_checkin.event = {"id": "evt-demo", "name": "Demo"}
    ctx.robot.speech = SimpleNamespace(drop_pending=Mock())
    try:
        assert ctx.camera.get_frame() is not None
        result = await ctx.start_luma_express()
        assert result["express"]["active"] and result["express"]["camera_released"]
        assert ctx.conversation.suspended
        ctx.robot.speech.drop_pending.assert_called_once()
        assert not ctx.audio_router._running
        assert not ctx.attention.enabled
        assert ctx.camera.get_frame() is None
        assert ctx.get_camera_snapshot() is None
        assert ctx.camera_diagnostics() == {"available": False, "reason": "camera_handed_to_luma"}
        with pytest.raises(RuntimeError, match="return the camera"):
            await ctx.start_voice_test()
        await ctx.conversation.handle_wake()
        await ctx.conversation.handle_command(CommandIntent.OPEN_DOOR)
        assert not ctx.conversation._engaged
        assert await ctx.conversation.request_access_check() == {"status": "unavailable"}
        for action in (ctx.event_checkin.start_scanning(), ctx.event_checkin.select_event("evt-other")):
            with pytest.raises(ValueError, match="return the camera"):
                await action
        with pytest.raises(ValueError, match="Close Luma"):
            await ctx.finish_luma_express(False)
        assert ctx.camera.get_frame() is None
        assert not (await ctx.finish_luma_express(True))["express"]["active"]
        assert not ctx.conversation.suspended
        assert not ctx.conversation._engaged  # returning the camera never opens a cloud session
        assert ctx.camera.get_frame() is not None
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_startup_restores_handoff_before_any_camera_capture(settings, tmp_path):
    settings.app_env = RuntimeMode.SIMULATION
    settings.data_dir = tmp_path
    marker = LumaExpressHandoff(tmp_path / "luma-browser-handoff.json")
    marker.begin("evt-demo")
    ctx = AppContext(settings=settings)
    ctx.audio_router.start = AsyncMock()
    ctx.doa = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    try:
        assert ctx.luma_express.active and ctx.camera.external_owner
        assert ctx.conversation.suspended
        assert ctx.event_checkin.event["id"] == "evt-demo"
        assert not ctx.conversation.diagnostics()["dashboard_auto_start"]
        assert ctx.camera.get_frame() is None
        await ctx.start()
        ctx.audio_router.start.assert_not_awaited()
        ctx.doa.start.assert_not_awaited()
    finally:
        await ctx.stop()
    assert marker.path.exists()  # closing the app cannot close Chrome's scanner


@pytest.mark.asyncio
async def test_partial_handoff_failure_leaves_voice_paused_and_can_be_recovered(settings):
    ctx = AppContext(settings=settings)
    ctx.event_checkin.event = {"id": "evt-demo", "name": "Demo"}
    ctx.robot.wake = AsyncMock(side_effect=RuntimeError("private device detail"))
    try:
        with pytest.raises(ValueError, match="did not finish") as error:
            await ctx.start_luma_express()
        assert "private device detail" not in str(error.value)
        assert ctx.luma_express.active and not ctx.luma_express.ready
        assert ctx.conversation.suspended and ctx.camera.external_owner
        await ctx.finish_luma_express(True)
        assert not ctx.luma_express.active
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_handoff_stops_local_audio_and_direction_workers_then_return_restarts(settings):
    ctx = AppContext(settings=settings)
    ctx.event_checkin.event = {"id": "evt-demo", "name": "Demo"}
    ctx.audio_router.start, ctx.audio_router.stop = AsyncMock(), AsyncMock()
    ctx.doa = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    try:
        await ctx.start_luma_express()
        ctx.audio_router.stop.assert_awaited_once()
        ctx.doa.stop.assert_awaited_once()
        ctx.audio_router.start.assert_not_awaited()
        await ctx.finish_luma_express(True)
        ctx.audio_router.start.assert_awaited_once()
        ctx.doa.start.assert_awaited_once()
        assert not ctx.conversation._engaged
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_failed_microphone_resume_keeps_handoff_paused_and_recoverable(settings):
    ctx = AppContext(settings=settings)
    ctx.event_checkin.event = {"id": "evt-demo", "name": "Demo"}
    try:
        await ctx.start_luma_express()
        ctx.audio_router.start = AsyncMock(side_effect=RuntimeError("private device detail"))
        with pytest.raises(ValueError, match="Chat stays paused"):
            await ctx.finish_luma_express(True)
        assert ctx.conversation.suspended and ctx.luma_express.active
        assert ctx.camera.external_owner and not ctx.audio_router._running
        ctx.audio_router.start = AsyncMock()
        await ctx.finish_luma_express(True)
        assert not ctx.luma_express.active
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_hardware_fault_cannot_open_express_or_move_robot(settings):
    ctx = AppContext(settings=settings)
    ctx.event_checkin.event = {"id": "evt-demo", "name": "Demo"}
    ctx.robot.wake = AsyncMock()
    await ctx.state_machine.transition(AppState.SAFE_HOLD)
    try:
        with pytest.raises(ValueError, match="hardware fault"):
            await ctx.start_luma_express()
        assert not ctx.luma_express.active
        ctx.robot.wake.assert_not_awaited()
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_camera_handoff_waits_for_read_and_does_not_block_event_loop(settings):
    entered, finish = threading.Event(), threading.Event()
    released = []
    reads = []

    def read():
        reads.append(True)
        entered.set()
        assert finish.wait(2)
        return np.zeros((10, 10, 3), dtype=np.uint8)

    camera = ReachyCameraAdapter(
        settings=settings,
        telemetry=None,
        media=SimpleNamespace(get_frame=read, release_idle_camera=lambda: released.append(True)),
    )
    capture = asyncio.create_task(asyncio.to_thread(camera.get_frame))
    await asyncio.to_thread(entered.wait, 1)
    handing_off = asyncio.create_task(asyncio.to_thread(camera.set_external_owner, True))
    try:
        await asyncio.sleep(0.02)
        assert not handing_off.done() and not released
        finish.set()
        await asyncio.wait_for(asyncio.gather(capture, handing_off), 1)
        assert released == [True]
        assert camera.get_frame() is None and camera.get_snapshot_bytes() is None
        assert len(reads) == 1
    finally:
        finish.set()


@pytest.mark.asyncio
async def test_handoff_finishes_inflight_wake_then_stops_and_rejects_future_wakes(settings):
    ctx = AppContext(settings=settings)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def prepare():
        entered.set()
        await finish.wait()

    ctx.conversation.prepare_engagement = prepare
    waking = asyncio.create_task(ctx.conversation.handle_wake())
    await entered.wait()
    suspending = asyncio.create_task(ctx.conversation.suspend())
    await asyncio.sleep(0)
    assert ctx.conversation.suspended
    finish.set()
    await asyncio.gather(waking, suspending)
    try:
        assert not ctx.conversation._engaged
        await ctx.conversation.handle_wake()
        assert not ctx.conversation._engaged
    finally:
        await ctx.stop()


def test_launcher_does_not_restart_chat_during_persisted_handoff(monkeypatch):
    request = Mock(return_value={"conversation": {"pause_reason": "luma_express"}})
    monkeypatch.setattr(launch_reachy, "request_json", request)
    assert launch_reachy.connect_chat() is False
    request.assert_called_once_with("http://127.0.0.1:8080/api/admin/diagnostics/audio")


def test_express_admin_api_requires_explicit_scanner_closed(settings):
    ctx = AppContext(settings=settings)
    ctx.event_checkin.event = {"id": "evt-demo", "name": "Demo"}
    app = FastAPI()
    app.include_router(router)
    app.state.context = ctx
    with TestClient(app) as client:
        assert client.post(
            "/api/admin/event-checkin/express/start", headers={"Origin": "https://example.com"}
        ).status_code == 403
        assert not ctx.luma_express.active
        response = client.post("/api/admin/event-checkin/express/start")
        assert response.status_code == 200 and response.json()["express"]["camera_released"]
        assert response.headers["cache-control"] == "no-store"
        assert client.post(
            "/api/admin/event-checkin/express/finish", json={"scanner_closed": "true"}
        ).status_code == 422
        assert client.post(
            "/api/admin/event-checkin/express/finish", json={"scanner_closed": True},
            headers={"Origin": "https://example.com"},
        ).status_code == 403
        assert ctx.luma_express.active
        assert (
            client.post("/api/admin/event-checkin/express/finish", json={"scanner_closed": False}).status_code
            == 409
        )
        assert (
            client.post("/api/admin/event-checkin/express/finish", json={"scanner_closed": True}).status_code
            == 200
        )
        client.portal.call(ctx.stop)
