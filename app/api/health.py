"""Health endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


def get_ctx(request: Request):
    return request.app.state.context


@router.get("/healthz/live")
async def live(request: Request) -> dict:
    return get_ctx(request).live_health()


@router.get("/healthz/ready")
async def ready(request: Request) -> JSONResponse:
    payload = get_ctx(request).readiness()
    return JSONResponse(payload, status_code=200 if payload["ready"] else 503)
