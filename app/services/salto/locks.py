"""SALTO lock access client."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.config import Settings
from app.models.domain import UnlockResult


class SaltoClient(Protocol):
    async def unlock(self, *, access_token: str, site_id: str, lock_id: str) -> UnlockResult: ...


class HttpSaltoClient:
    """Minimal SALTO API client."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=settings.salto_api_base_url.rstrip("/"), timeout=10.0, transport=transport)

    async def close(self) -> None:
        await self.client.aclose()

    async def unlock(self, *, access_token: str, site_id: str, lock_id: str) -> UnlockResult:
        response = await self.client.post(
            f"/sites/{site_id}/locks/{lock_id}/unlock",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"reason": "reachy-office-bouncer"},
        )
        if response.status_code >= 400:
            return UnlockResult(success=False, detail=f"unlock_failed:{response.status_code}", metadata={"body": response.text})
        payload = response.json() if response.content else {}
        return UnlockResult(
            success=True,
            detail="unlocked",
            provider_request_id=str(payload.get("request_id", "")) or None,
            metadata=payload,
        )


class MockSaltoClient:
    """Mock SALTO client."""

    def __init__(self, *, fail_auth: bool = False, fail_unlock: bool = False) -> None:
        self.fail_auth = fail_auth
        self.fail_unlock = fail_unlock
        self.calls: list[dict[str, str]] = []

    async def unlock(self, *, access_token: str, site_id: str, lock_id: str) -> UnlockResult:
        self.calls.append({"access_token": access_token, "site_id": site_id, "lock_id": lock_id})
        if self.fail_auth:
            return UnlockResult(success=False, detail="unauthorized")
        if self.fail_unlock:
            return UnlockResult(success=False, detail="lock_offline")
        return UnlockResult(success=True, detail="unlocked", provider_request_id="mock-request")
