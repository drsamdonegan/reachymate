"""Fixed-origin front-desk HTTP client, with bounded responses and circuit breaking."""

from __future__ import annotations

import time
from collections import deque
from urllib.parse import urlsplit
from uuid import UUID

import httpx

STATUSES = {"pending", "host_coming", "declined", "expired", "approved_entry"}


def validate_origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Front-desk origin cannot contain credentials, query or fragment")
    if parsed.path not in {"", "/"} or not parsed.hostname:
        raise ValueError("Front-desk requires an origin")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Front-desk must use HTTPS or loopback HTTP")
    return url.rstrip("/")


class FrontdeskHttpClient:
    def __init__(self, settings, *, transport=None):
        token = settings.frontdesk_token
        self.client = httpx.AsyncClient(
            base_url=validate_origin(settings.frontdesk_url),
            timeout=3,
            follow_redirects=False,
            transport=transport,
            headers={"Authorization": f"Bearer {token.get_secret_value()}"} if token else {},
        )
        self.failures = deque()
        self.disabled_until = 0.0

    async def _request(self, method, path, **kwargs):
        if time.monotonic() < self.disabled_until:
            return None
        try:
            async with self.client.stream(method, path, **kwargs) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 65536:
                        raise ValueError("Front-desk response too large")
                import json

                payload = json.loads(data)
                if not isinstance(payload, dict) or payload.get("status") not in STATUSES:
                    raise ValueError("Malformed front-desk response")
                ref = str(UUID(payload["request_ref"]))
                return {"request_ref": ref, "status": payload["status"]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            now = time.monotonic()
            self.failures.append(now)
            while self.failures and self.failures[0] < now - 600:
                self.failures.popleft()
            if len(self.failures) >= 3:
                self.disabled_until = now + 300
            return None

    async def notify(self, payload, idempotency_key):
        return await self._request(
            "POST",
            "/api/v1/front-door/visitor-requests",
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )

    async def status(self, ref):
        ref = str(UUID(ref))
        return await self._request("GET", f"/api/v1/front-door/visitor-requests/{ref}")

    async def close(self):
        await self.client.aclose()
