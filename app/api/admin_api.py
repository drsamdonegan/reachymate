"""Admin and diagnostics API."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool

from app.services.reachy.motion.commands import RobotMotionRequest

router = APIRouter(prefix="/api/admin")


def get_ctx(request: Request):
    return request.app.state.context


class EventSelection(BaseModel):
    event_id: str = Field(min_length=5, max_length=84)


class EventScan(BaseModel):
    qr_code: str = Field(min_length=1, max_length=2048, repr=False)


class EventVerification(BaseModel):
    scan_id: str = Field(min_length=1, max_length=64)


class CameraReturn(BaseModel):
    scanner_closed: StrictBool


class HostWelcome(BaseModel):
    greeting: str = Field(min_length=1, max_length=300)


class HostCamera(BaseModel):
    camera_label: str = Field(min_length=1, max_length=120)


def require_dashboard_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Use the local Reachy dashboard for camera handoff")


@router.post("/event-checkin/express/start")
async def luma_express_start(request: Request):
    require_dashboard_origin(request)
    try:
        return private_result(await get_ctx(request).start_luma_express())
    except (ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Could not save the camera handoff state"
        raise HTTPException(409, message) from None


@router.post("/event-checkin/express/finish")
async def luma_express_finish(request: Request, payload: CameraReturn):
    require_dashboard_origin(request)
    try:
        return private_result(await get_ctx(request).finish_luma_express(payload.scanner_closed))
    except (ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Could not clear the camera handoff state"
        raise HTTPException(409, message) from None


def private_result(value):
    return JSONResponse(value, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/event-greeter/start")
async def event_greeter_start(request: Request, payload: HostWelcome | None = None):
    require_dashboard_origin(request)
    try:
        return private_result(
            await get_ctx(request).start_event_greeter(payload.greeting if payload else None)
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/event-greeter/stop")
async def event_greeter_stop(request: Request):
    require_dashboard_origin(request)
    return private_result(await get_ctx(request).stop_event_greeter())


@router.post("/event-checkin/host/start")
async def luma_host_start(request: Request, payload: HostWelcome):
    require_dashboard_origin(request)
    try:
        return private_result(await get_ctx(request).start_luma_host(payload.greeting))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/event-checkin/host/stop")
async def luma_host_stop(request: Request):
    require_dashboard_origin(request)
    return private_result(await get_ctx(request).stop_luma_host())


@router.post("/event-checkin/host/welcome")
async def luma_host_welcome(request: Request):
    require_dashboard_origin(request)
    try:
        return private_result(await get_ctx(request).welcome_luma_guest())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/event-checkin/host/camera/start")
async def host_camera_start(request: Request, payload: HostCamera):
    require_dashboard_origin(request)
    ctx = get_ctx(request)
    if not (ctx.settings.camera_vision_enabled and ctx.luma_express.host_voice and ctx.realtime.running):
        raise HTTPException(409, "Start the GPT-Live host with camera vision enabled first")
    try:
        return private_result({"token": ctx.browser_camera.start(payload.camera_label)})
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


def browser_camera_auth(request: Request):
    require_dashboard_origin(request)
    ctx = get_ctx(request)
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not ctx.browser_camera.authorize(token):
        raise HTTPException(403, "Camera sharing ended; enable it again")
    return ctx, token


@router.get("/event-checkin/host/camera/request")
async def host_camera_request(request: Request):
    ctx, token = browser_camera_auth(request)
    if not ctx._model_vision_allowed():
        raise HTTPException(409, "Camera sharing is paused")
    return private_result(ctx.browser_camera.poll(token))


@router.post("/event-checkin/host/camera/frame/{request_id}")
async def host_camera_frame(request: Request, request_id: str):
    from app.services.vision.browser_camera import MAX_JPEG_BYTES

    ctx, token = browser_camera_auth(request)
    if not ctx._model_vision_allowed():
        raise HTTPException(409, "Camera sharing is paused")
    if request.headers.get("content-type") != "image/jpeg":
        raise HTTPException(415, "Expected a camera JPEG")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_JPEG_BYTES:
            raise HTTPException(413, "Camera image too large")
    try:
        await ctx.browser_camera.submit(token, request_id, bytes(content))
    except (ValueError, OSError):
        raise HTTPException(409, "Camera frame invalid, expired or cancelled") from None
    return private_result({"status": "received"})


@router.post("/event-checkin/host/camera/stop")
async def host_camera_stop(request: Request):
    ctx, _ = browser_camera_auth(request)
    ctx.browser_camera.stop()
    return private_result({"status": "stopped"})


async def event_action(request, action, *args):
    from app.services.events.luma import LumaError

    try:
        return private_result(await getattr(get_ctx(request).event_checkin, action)(*args))
    except (ValueError, LumaError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/event-checkin")
async def event_checkin_status(request: Request):
    return private_result(get_ctx(request).event_checkin.snapshot())


@router.post("/event-checkin/event")
async def event_checkin_event(request: Request, payload: EventSelection):
    return await event_action(request, "select_event", payload.event_id)


@router.post("/event-checkin/start")
async def event_checkin_start(request: Request):
    return await event_action(request, "start_scanning")


@router.post("/event-checkin/stop")
async def event_checkin_stop(request: Request):
    return await event_action(request, "stop_scanning")


@router.post("/event-checkin/scan")
async def event_checkin_scan(request: Request, payload: EventScan):
    return await event_action(request, "scan", payload.qr_code)


@router.post("/event-checkin/verify")
async def event_checkin_verify(request: Request, payload: EventVerification):
    return await event_action(request, "verify", payload.scan_id)


@router.post("/event-checkin/next")
async def event_checkin_next(request: Request):
    return await event_action(request, "next_guest")


class EnrollmentSample(BaseModel):
    label: str | None = None
    image_b64: str


class EnrollmentRequest(BaseModel):
    full_name: str
    slack_user_id: str
    slack_display_name: str
    company: str | None = None
    biometric_consent: bool
    operator_id: str = Field(min_length=1, max_length=64)
    notice_version: str = Field(min_length=1, max_length=64)
    notice_sha256: str = Field(min_length=64, max_length=64)
    greet_by_name: bool = False
    reinstate: bool = False
    samples: list[EnrollmentSample] = Field(min_length=1)


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=240)


@router.post("/enroll")
async def enroll(request: Request, payload: EnrollmentRequest) -> dict[str, Any]:
    try:
        return await get_ctx(request).enroll(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/biometric-notice")
async def biometric_notice():
    import hashlib

    from app.services.conversation.fallback_audio import CONSENT_NOTICE

    return {
        "notice_version": "biometric-notice-v1",
        "notice_text": CONSENT_NOTICE,
        "notice_sha256": hashlib.sha256(CONSENT_NOTICE.encode()).hexdigest(),
    }


@router.get("/events")
async def events(request: Request, limit: int = Query(default=50, le=100)) -> list[dict[str, Any]]:
    return get_ctx(request).recent_events(limit=limit)


@router.get("/diagnostics/audio")
async def audio_diagnostics(request: Request) -> dict[str, Any]:
    return get_ctx(request).audio_diagnostics()


@router.get("/diagnostics/camera")
async def camera_diagnostics(request: Request) -> dict[str, Any]:
    return await asyncio.to_thread(get_ctx(request).camera_diagnostics)


@router.get("/diagnostics/telemetry")
async def telemetry_diagnostics(request: Request) -> list[dict[str, Any]]:
    return get_ctx(request).telemetry_snapshot()


@router.get("/users")
async def users(request: Request) -> list[dict[str, Any]]:
    return get_ctx(request).list_users()


@router.get("/users/{user_id}/booking-check")
async def booking_check(request: Request, user_id: str) -> dict[str, Any]:
    try:
        return await get_ctx(request).booking_check(user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="User not found") from exc


@router.post("/test-speech")
async def test_speech(request: Request, payload: SpeechRequest) -> dict[str, str]:
    try:
        await get_ctx(request).speak(payload.text)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "queued"}


@router.post("/test-conversation")
async def test_conversation(request: Request) -> dict[str, Any]:
    try:
        return await get_ctx(request).start_voice_test()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/robot/motions")
async def robot_motions(request: Request) -> dict[str, Any]:
    return get_ctx(request).robot_motion_capabilities()


@router.post("/robot/motions/reload")
async def reload_robot_motions(request: Request):
    require_dashboard_origin(request)
    reload = getattr(get_ctx(request).robot, "reload_movements", None)
    if reload is None:
        raise HTTPException(409, "Movement library is unavailable in this profile")
    try:
        return private_result(await reload())
    except (ValueError, TypeError, KeyError, OSError):
        raise HTTPException(409, "Could not load the movement catalogue; check provisioning") from None


@router.post("/conversation/stop")
async def stop_conversation(request: Request) -> dict[str, str]:
    return await get_ctx(request).stop_voice_test()


@router.post("/conversation/request")
async def conversation_request(request: Request, payload: SpeechRequest) -> dict[str, str]:
    try:
        return await get_ctx(request).send_voice_request(payload.text)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/robot/motion")
async def robot_motion(request: Request, payload: RobotMotionRequest) -> dict[str, Any]:
    return await get_ctx(request)._robot_motion(payload.model_dump(exclude_none=True))


@router.post("/simulate/attempt")
async def simulate_attempt(request: Request) -> dict[str, str]:
    try:
        await get_ctx(request).simulate_attempt()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "started"}


class DemoModeRequest(BaseModel):
    enabled: bool
    expires_in_seconds: int = Field(default=3600, ge=1, le=14400)


@router.post("/attended-demo")
async def attended_demo(request: Request, payload: DemoModeRequest):
    import time

    ctx = get_ctx(request)
    ctx.settings.attended_demo = payload.enabled
    ctx.settings.attended_demo_expires_at = time.time() + payload.expires_in_seconds if payload.enabled else 0
    return {"attended_demo": payload.enabled, "expires_at": ctx.settings.attended_demo_expires_at}


@router.post("/fault/{fault}")
async def inject_fault(request: Request, fault: str):
    ctx = get_ctx(request)
    if ctx.reachy_media is None or fault not in {"audio_stall", "zero_audio"}:
        raise HTTPException(400, "Unsupported fault")
    ctx.reachy_media.inject_fault(fault)
    return {"status": "injected"}


@router.get("/selftest/tcc")
async def tcc_selftest():
    from app.services.system.tcc import permission_status

    return permission_status()
