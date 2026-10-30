from __future__ import annotations

import asyncio
import base64
import io
import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.models.enums import AppState
from app.runtime import AppContext
from app.services.audio.barge_in_guard import BargeInGuard
from app.services.live.session import build_session_start
from app.services.vision.camera import ReachyCameraAdapter
from app.services.vision.model_view import capture_model_view
from app.tests.unit.test_live_session import audio, backend, session_fixture


def snapshot():
    # Camera frames are BGR: ensure the model receives red, not blue.
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    frame[:, :, 2] = 255
    return capture_model_view(SimpleNamespace(get_frame=lambda: frame))


def test_snapshot_preserves_colors_and_is_bounded_in_memory():
    view = snapshot()
    decoded = Image.open(io.BytesIO(view.jpeg))
    assert max(decoded.size) == 1280 and len(view.jpeg) <= 512 * 1024
    red, green, blue = decoded.getpixel((10, 10))
    assert red > 245 and max(green, blue) < 5
    assert "jpeg=" not in repr(view)
    assert view.source == "Reachy Mini Camera"


def test_camera_schema_and_instructions_only_enabled_explicitly(settings):
    default = build_session_start(settings)["session"]
    assert "inspect_camera" not in str(default["delegation"]["responses"]["tools"])
    assert "You have access to Reachy Mini Camera" not in default["instructions"]
    settings.camera_vision_enabled = True
    config = build_session_start(settings)["session"]
    assert len(config["delegation"]["responses"]["tools"]) == 9
    assert "inspect_camera" in config["instructions"]
    assert "input_image" not in str(config["input"])


@pytest.mark.asyncio
async def test_image_is_sent_after_tool_outputs_and_before_backend_continuation(settings):
    settings.camera_vision_enabled = True
    session, wire, _, _ = session_fixture(settings)
    view = snapshot()

    async def capture():
        return view

    session.vision_snapshot_provider = capture
    session.tools.set_handler("inspect_camera", lambda arguments, _: {"status": "ready", **arguments})
    await session.start()
    try:
        await session._execute_live_tool_batch(
            [
                {
                    "call_id": "camera1",
                    "name": "inspect_camera",
                    "arguments": '{"question":"What can you see?"}',
                },
                {"call_id": "wait1", "name": "wait_for_user", "arguments": "{}"},
            ],
            session._generation,
        )
        items = [e["item"] for e in wire.sent if e["type"] == "response.item.create"]
        assert [item["type"] for item in items] == ["function_call_output", "function_call_output", "message"]
        assert json.loads(items[0]["output"])["status"] == "captured"
        image = items[2]["content"][1]
        assert image["type"] == "input_image"
        assert base64.b64decode(image["image_url"].split(",", 1)[1]) == view.jpeg
        assert "image_url" not in str(items[:2])
        assert [e for e in wire.sent if e["type"] == "response.create"]
        assert session.diagnostics()["vision"]["images_sent"] == 1
        await backend(session, "response.created", response_id="visual", response={"id": "visual"})
        await backend(
            session,
            "response.output_item.done",
            response_id="visual",
            item={
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "A red rectangle."}],
            },
        )
        assert session.diagnostics()["vision"]["last_observation"] == "A red rectangle."
        telemetry = json.dumps(session.telemetry.snapshot())
        assert "base64" not in telemetry and "A red rectangle" not in telemetry
    finally:
        await session.stop()
    assert session.diagnostics()["vision"]["last_observation"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["camera_failed", "invalid_arguments", "consent"])
async def test_camera_failure_or_blocked_capture_never_breaks_voice(settings, failure):
    settings.camera_vision_enabled = True
    session, wire, _, _ = session_fixture(settings)
    captures = []

    async def capture():
        captures.append(True)
        raise RuntimeError("private device detail")

    session.vision_snapshot_provider = capture
    session.tools.set_handler("inspect_camera", lambda arguments, _: {"status": "ready", **arguments})
    await session.start()
    try:
        if failure == "consent":
            session.suspend_input()
        arguments = {"question": "What is here?"}
        if failure == "invalid_arguments":
            arguments["image_url"] = "https://untrusted.invalid/private.jpg"
        await session._execute_live_tool_batch(
            [{"call_id": "camera1", "name": "inspect_camera", "arguments": json.dumps(arguments)}],
            session._generation,
        )
        assert session.running
        assert bool(captures) == (failure == "camera_failed")
        assert not any("input_image" in json.dumps(e) for e in wire.sent)
        assert "private device detail" not in json.dumps(session.telemetry.snapshot())
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_camera_work_does_not_block_audio_and_late_images_are_discarded(settings):
    settings.camera_vision_enabled = True
    session, wire, _, _ = session_fixture(settings)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def capture():
        entered.set()
        await finish.wait()
        return snapshot()

    session.vision_snapshot_provider = capture
    session.tools.set_handler("inspect_camera", lambda arguments, _: {"status": "ready", **arguments})
    await session.start()
    batch = asyncio.create_task(
        session._execute_live_tool_batch(
            [{"call_id": "camera1", "name": "inspect_camera", "arguments": '{"question":"What is here?"}'}],
            session._generation,
        )
    )
    await entered.wait()
    await session.handle_server_event(audio(np.full(320, 500, dtype=np.int16).tobytes()))
    assert session.diagnostics()["output_frames_received"] == 320
    await session.stop()
    finish.set()
    await batch
    assert not any("input_image" in json.dumps(e) for e in wire.sent)


@pytest.mark.asyncio
async def test_stop_cancels_an_image_already_waiting_for_the_socket_lock(settings):
    session, wire, _, _ = session_fixture(settings)
    await session.start()
    await session._send_lock.acquire()
    image = asyncio.create_task(
        session._send_event(
            {"type": "response.item.create", "item": {"type": "input_image"}},
            image_generation=session._generation,
        )
    )
    stopping = asyncio.create_task(session.stop())
    async with asyncio.timeout(1):
        while not session._live_closing:
            await asyncio.sleep(0)
    session._send_lock.release()
    assert not await image
    await stopping
    assert not any("input_image" in json.dumps(e) for e in wire.sent)


@pytest.mark.asyncio
async def test_qr_scanning_blocks_an_image_already_waiting_for_the_socket_lock(settings):
    session, wire, _, _ = session_fixture(settings)
    allowed = True
    session.vision_allowed = lambda: allowed
    await session.start()
    await session._send_lock.acquire()
    image = asyncio.create_task(
        session._send_event(
            {"type": "response.item.create", "item": {"type": "input_image"}},
            image_generation=session._generation,
        )
    )
    await asyncio.sleep(0)
    allowed = False
    session._send_lock.release()
    assert not await image
    assert session.running
    assert not any("input_image" in json.dumps(e) for e in wire.sent)
    await session.stop()


@pytest.mark.asyncio
async def test_runtime_vision_requires_active_attended_chat_and_reachy_camera(settings):
    settings.camera_vision_enabled = True
    context = AppContext.__new__(AppContext)
    context.settings = settings
    context.conversation = SimpleNamespace(attended_chat_active=True)
    context.state_machine = SimpleNamespace(state=AppState.ENGAGED)
    context.attention = SimpleNamespace(enabled=True)
    context.event_checkin = SimpleNamespace(scanning=False)
    context.robot = SimpleNamespace(motion=None)
    context.guard = BargeInGuard(0)
    context.guard.begin_engagement(10)
    context.realtime = SimpleNamespace(running=True, _input_suspended=False)
    reads = []

    def frame():
        reads.append(True)
        return np.zeros((20, 20, 3), dtype=np.uint8)

    context.camera = ReachyCameraAdapter(
        settings=settings, telemetry=None, media=SimpleNamespace(get_frame=frame)
    )
    assert (await context._inspect_camera({"question": "What is here?"}, "c1"))["status"] == "ready"
    assert await context._capture_model_view() is not None
    for condition in ("consent", "event_scan", "idle", "wrong_camera"):
        before = len(reads)
        context.realtime._input_suspended = condition == "consent"
        context.event_checkin.scanning = condition == "event_scan"
        context.conversation.attended_chat_active = condition != "idle"
        if condition == "wrong_camera":
            context.camera = SimpleNamespace(get_frame=frame)
        assert await context._capture_model_view() is None
        assert len(reads) == before
