"""FastAPI entrypoint."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import admin_api, health
from app.config import Settings, get_settings
from app.logging_setup import configure_logging
from app.runtime import AppContext


def create_app(
    settings: Settings | None = None,
    *,
    context_factory: Callable[[Settings], AppContext] | None = None,
) -> FastAPI:
    """Create the FastAPI app."""

    settings = settings or get_settings()
    configure_logging(settings)
    static_dir = settings.static_dir

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        context = context_factory(settings) if context_factory is not None else AppContext(settings=settings)
        app.state.context = context
        await context.start()
        try:
            yield
        finally:
            await context.stop()

    app = FastAPI(title="Reachy Office Bouncer", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(health.router)
    app.include_router(admin_api.router)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/camera/snapshot")
    async def camera_snapshot(request: Request) -> Response:
        content = request.app.state.context.get_camera_snapshot()
        if content is None:
            return Response(status_code=503)
        return Response(content=content, media_type="image/jpeg")

    @app.websocket("/ws/telemetry")
    async def telemetry_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = await websocket.app.state.context.telemetry.subscribe()
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            websocket.app.state.context.telemetry.unsubscribe(queue)

    return app


def main() -> None:
    settings = get_settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port)


app = create_app()

