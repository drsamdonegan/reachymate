"""Admin and diagnostics API."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/admin")


def get_ctx(request: Request):
    return request.app.state.context


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
