"""Health endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request


router = APIRouter()


def get_ctx(request: Request):
    return request.app.state.context


@router.get("/healthz/live")
async def live(request: Request) -> dict:
    return get_ctx(request).live_health()


@router.get("/healthz/ready")
async def ready(request: Request) -> dict:
    return get_ctx(request).readiness()
