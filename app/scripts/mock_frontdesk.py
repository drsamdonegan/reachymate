"""Loopback-only mock. No Slack calls, names or contact details persisted."""

from __future__ import annotations

import argparse
import time
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict


class MockRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    request_ref: str
    device_id: str
    session_ref: str
    intent: str


def create_mock(*, reply="host_coming", reply_delay=3):
    app = FastAPI(title="Mock MLAI front desk")
    requests = {}
    replay = {}

    def notify(payload, key):
        if key in replay:
            return replay[key]
        ref = str(UUID(payload["request_ref"]))
        requests[ref] = time.monotonic()
        result = {"request_ref": ref, "status": "pending"}
        replay[key] = result
        return result

    def status(ref):
        if ref not in requests:
            raise KeyError(ref)
        return {
            "request_ref": ref,
            "status": reply if time.monotonic() - requests[ref] >= reply_delay else "pending",
        }

    @app.post("/api/v1/front-door/visitor-requests")
    async def post(payload: MockRequest, idempotency_key: str = Header()):
        return notify(payload.model_dump(), idempotency_key)

    @app.get("/api/v1/front-door/visitor-requests/{ref}")
    async def get(ref: str):
        try:
            return status(ref)
        except KeyError as exc:
            raise HTTPException(404, "Request unavailable") from exc

    try:
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("mlai-frontdesk-mock", stateless_http=True)

        @mcp.tool()
        async def notify_host(payload: dict, idempotency_key: str) -> dict:
            return notify(payload, idempotency_key)

        @mcp.tool()
        async def notify_channel(payload: dict, idempotency_key: str) -> dict:
            return notify(payload, idempotency_key)

        @mcp.tool()
        async def get_request_status(request_ref: str) -> dict:
            return status(request_ref)

        mcp_app = mcp.streamable_http_app()
        app.mount("/", mcp_app)
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(_app):
            async with mcp.session_manager.run():
                yield

        app.router.lifespan_context = lifespan
    except ImportError:
        pass
    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--reply", choices=["host_coming", "declined", "expired"], default="host_coming")
    args = parser.parse_args()
    uvicorn.run(create_mock(reply=args.reply), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
