"""Admin and diagnostics API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
    samples: list[EnrollmentSample] = Field(min_length=1)


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=240)


@router.post("/enroll")
async def enroll(request: Request, payload: EnrollmentRequest) -> dict[str, Any]:
    try:
        return await get_ctx(request).enroll(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/events")
async def events(request: Request, limit: int = Query(default=50, le=100)) -> list[dict[str, Any]]:
    return get_ctx(request).recent_events(limit=limit)


@router.get("/diagnostics/audio")
async def audio_diagnostics(request: Request) -> dict[str, Any]:
    return get_ctx(request).audio_diagnostics()


@router.get("/diagnostics/camera")
async def camera_diagnostics(request: Request) -> dict[str, Any]:
    return get_ctx(request).camera_diagnostics()


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
    await get_ctx(request).speak(payload.text)
    return {"status": "queued"}


@router.post("/simulate/attempt")
async def simulate_attempt(request: Request) -> dict[str, str]:
    await get_ctx(request).simulate_attempt()
    return {"status": "started"}


@router.get("/salto/oauth/start")
async def salto_oauth_start(request: Request) -> RedirectResponse:
    return RedirectResponse(get_ctx(request).salto_authorize_url())


@router.get("/salto/oauth/callback")
async def salto_oauth_callback(request: Request, code: str, state: str) -> HTMLResponse:
    try:
        token_payload = await get_ctx(request).handle_salto_callback(code=code, state=state)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return HTMLResponse(
        "<html><body><h1>SALTO Connected</h1>"
        f"<p>Access token stored. Token type: {token_payload.get('token_type', 'Bearer')}</p>"
        "</body></html>"
    )
