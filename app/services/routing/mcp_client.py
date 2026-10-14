"""Small MCP 1.x client. Fixed endpoint and tool allowlist; model cannot select either."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from importlib.metadata import version

from .frontdesk_http import STATUSES, validate_origin


class FrontdeskMcpClient:
    ALLOWED = frozenset(
        {"notify_host", "notify_channel", "get_request_status", "lookup_host", "event_lookup"}
    )

    def __init__(self, settings):
        self.url = validate_origin(settings.frontdesk_url) + "/mcp"
        self.token = settings.frontdesk_token
        self.failures = deque()
        self.disabled_until = 0.0

    async def call(self, tool, arguments):
        if (
            tool not in self.ALLOWED
            or len(json.dumps(arguments)) > 4096
            or time.monotonic() < self.disabled_until
        ):
            return None
        try:
            async with asyncio.timeout(3):
                if not version("mcp").startswith("1."):
                    raise RuntimeError("Front-desk requires MCP 1.x")
                from mcp import ClientSession
                from mcp.client.streamable_http import streamablehttp_client

                headers = {"Authorization": f"Bearer {self.token.get_secret_value()}"} if self.token else {}
                # AnyIO scopes must be entered/exited by the same caller task.
                async with streamablehttp_client(self.url, headers=headers) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool, arguments)
                if result.isError:
                    raise ValueError("Front-desk tool failed")
                raw = "".join(getattr(part, "text", "") for part in result.content)
                if len(raw) > 65536:
                    raise ValueError("Front-desk response too large")
                payload = json.loads(raw)
                if payload.get("status") not in STATUSES:
                    raise ValueError("Malformed front-desk response")
                from uuid import UUID

                return {"request_ref": str(UUID(payload["request_ref"])), "status": payload["status"]}
        except Exception:
            now = time.monotonic()
            self.failures.append(now)
            while self.failures and self.failures[0] < now - 600:
                self.failures.popleft()
            if len(self.failures) >= 3:
                self.disabled_until = now + 300
            return None

    async def notify(self, payload, idempotency_key):
        tool = payload.get("skill_tool")
        if tool not in {"notify_host", "notify_channel"}:
            return None
        return await self.call(tool, {"payload": payload, "idempotency_key": idempotency_key})

    async def status(self, ref):
        return await self.call("get_request_status", {"request_ref": ref})

    async def close(self):
        return None  # Each bounded call owns and closes its transport.
