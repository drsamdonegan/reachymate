from __future__ import annotations

import asyncio
import io
import json
import time

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from app.api.admin_api import router
from app.services.vision.browser_camera import MAX_JPEG_BYTES, BrowserCameraBridge, decode_browser_view
from app.tests.unit.test_live_session import until
from app.tests.unit.test_luma_host import host_context


def jpeg(size=(40, 30), format="JPEG"):
    stream = io.BytesIO()
    Image.new("RGB", size, "red").save(stream, format=format)
    return stream.getvalue()


@pytest.mark.parametrize(
    "content", [b"bad", b"x" * (MAX_JPEG_BYTES + 1), jpeg((1281, 1)), jpeg(format="PNG")]
)
def test_browser_frames_reject_invalid_format_dimensions_and_size(content):
    with pytest.raises((ValueError, OSError)):
        decode_browser_view(content)


@pytest.mark.asyncio
async def test_bridge_only_accepts_requested_frames_and_expires_or_cancels_them():
    bridge = BrowserCameraBridge()
    with pytest.raises(ValueError, match="Select Reachy"):
        bridge.start("MacBook Camera")
    token = bridge.start("Reachy Mini Camera (USB)")
    assert bridge.active and bridge.authorize(token)
    assert token not in json.dumps(bridge.diagnostics())
    with pytest.raises(ValueError, match="already"):
        bridge.start("Reachy Mini Camera")
    assert bridge.poll(token) == {"request_id": None}
    with pytest.raises(ValueError, match="expired"):
        await bridge.submit(token, "unsolicited", jpeg())
    capture = asyncio.create_task(bridge.capture())
    await until(lambda: bridge.poll(token)["request_id"] is not None)
    request_id = bridge.poll(token)["request_id"]
    with pytest.raises(ValueError):
        await bridge.submit("wrong-token", request_id, jpeg())
    await bridge.submit(token, request_id, jpeg())
    view = await capture
    assert (view.width, view.height) == (40, 30)
    assert Image.open(io.BytesIO(view.jpeg)).getpixel((0, 0))[0] > 245
    assert bridge.poll(token)["request_id"] is None
    with pytest.raises(ValueError):
        await bridge.submit(token, request_id, jpeg())
    capture = asyncio.create_task(bridge.capture())
    await until(lambda: bridge.poll(token)["request_id"] is not None)
    bridge.stop()
    assert await capture is None and not bridge.authorize(token)
    token = bridge.start("Reachy Mini Camera")
    bridge._seen_at = time.monotonic() - bridge.LEASE_SECONDS - 1
    assert not bridge.active and await bridge.capture() is None
    with pytest.raises(ValueError):
        bridge.poll(token)


@pytest.mark.asyncio
async def test_late_decoding_is_discarded_after_bridge_stop(monkeypatch):
    bridge = BrowserCameraBridge()
    token = bridge.start("Reachy Mini Camera")
    capture = asyncio.create_task(bridge.capture())
    await until(lambda: bridge.poll(token)["request_id"] is not None)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def decode(*args):
        entered.set()
        await finish.wait()
        return decode_browser_view(jpeg())

    monkeypatch.setattr("app.services.vision.browser_camera.asyncio.to_thread", decode)
    upload = asyncio.create_task(bridge.submit(token, bridge.poll(token)["request_id"], jpeg()))
    await entered.wait()
    bridge.stop()
    finish.set()
    with pytest.raises(ValueError):
        await upload
    assert await capture is None


@pytest.mark.asyncio
async def test_host_camera_api_and_model_image_keep_native_camera_closed(settings):
    settings.camera_vision_enabled = True
    ctx, wire = host_context(settings)
    ctx.realtime.tools.set_handler("inspect_camera", ctx._inspect_camera)
    ctx.realtime.vision_snapshot_provider = ctx._capture_model_view
    ctx.realtime.vision_allowed = ctx._model_vision_allowed
    app = FastAPI()
    app.include_router(router)
    app.state.context = ctx
    base = "/api/admin/event-checkin/host/camera"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            assert (
                await c.post(base + "/start", json={"camera_label": "Reachy Mini Camera"})
            ).status_code == 409
            await ctx.start_luma_express()
            await ctx.start_luma_host("Welcome")
            assert not ctx._model_vision_allowed()
            assert (await ctx._inspect_camera({"question": "What is here?"}, "off"))[
                "reason"
            ] == "enable_camera_sharing_in_dashboard"
            denied = await c.post(
                base + "/start",
                json={"camera_label": "Reachy Mini Camera"},
                headers={"Origin": "https://wrong.example"},
            )
            assert denied.status_code == 403
            assert (await c.post(base + "/start", json={"camera_label": "MacBook Camera"})).status_code == 409
            started = await c.post(base + "/start", json={"camera_label": "Reachy Mini Camera"})
            assert started.headers["cache-control"] == "no-store"
            token = started.json()["token"]
            headers = {"Authorization": "Bearer " + token}
            assert ctx._model_vision_allowed()
            assert ctx.camera.get_frame() is None and not ctx.attention.enabled
            assert (await c.get(base + "/request")).status_code == 403
            batch = asyncio.create_task(
                ctx.realtime._execute_live_tool_batch(
                    [
                        {
                            "name": "inspect_camera",
                            "call_id": "camera1",
                            "arguments": '{"question":"What color is this?"}',
                        }
                    ],
                    ctx.realtime._generation,
                )
            )
            await until(lambda: ctx.browser_camera.poll(token)["request_id"] is not None)
            pending = (await c.get(base + "/request", headers=headers)).json()["request_id"]
            path = base + "/frame/" + pending
            assert (await c.post(path, headers=headers, content=jpeg())).status_code == 415
            headers["Content-Type"] = "image/jpeg"
            assert (
                await c.post(path, headers=headers, content=b"x" * (MAX_JPEG_BYTES + 1))
            ).status_code == 413
            assert (await c.post(path, headers=headers, content=b"invalid")).status_code == 409
            assert (await c.post(path, headers=headers, content=jpeg())).status_code == 200
            await batch
            assert ctx.realtime.diagnostics()["vision"]["images_sent"] == 1
            assert any("input_image" in json.dumps(event) for event in wire.sent)
            assert ctx.realtime.running and ctx.luma_express.active and ctx.camera.get_frame() is None
            assert (await c.post(base + "/stop", headers=headers)).status_code == 200
            assert not ctx._model_vision_allowed()
            assert (await c.post(path, headers=headers, content=jpeg())).status_code == 403
            ctx.browser_camera.start("Reachy Mini Camera")
            await ctx.stop_luma_host()
            assert not ctx.browser_camera.active and ctx.luma_express.active
    finally:
        await ctx.stop()
