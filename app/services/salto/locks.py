"""SALTO lock access client."""

from __future__ import annotations

from typing import ClassVar, Protocol

import httpx

from app.config import Settings
from app.models.domain import UnlockResult


class SaltoClient(Protocol):
    is_physical: bool

    async def unlock(self, *, access_token: str, site_id: str, lock_id: str) -> UnlockResult: ...


class HttpSaltoClient:
    """Typed subset of the current SALTO KS Connect locking contract."""

    is_physical: ClassVar[bool] = True

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.salto_api_base_url.rstrip("/"), timeout=10.0, transport=transport
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def unlock(self, *, access_token: str, site_id: str, lock_id: str) -> UnlockResult:
        response = await self.client.patch(
            f"/v1.2/sites/{site_id}/locks/{lock_id}/locking",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"locked_state": "unlocked"},
        )
        if response.status_code >= 400:
            return UnlockResult(
                success=False,
                detail=f"unlock_failed:{response.status_code}",
                metadata={"http_status": response.status_code},
            )
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            return UnlockResult(
                success=False,
                detail="unlock_response_malformed",
                metadata={"http_status": response.status_code},
            )
        if not isinstance(payload, dict):
            return UnlockResult(success=False, detail="unlock_response_malformed")
        request_id = response.headers.get("x-request-id") or payload.get("request_id")
        return UnlockResult(
            # SALTO's HTTP response accepts the remote-locking action; it does
            # not prove the door physically opened. A future door-sensor
            # reconciler must provide that confirmation before ALLOW.
            success=False,
            detail="unlock_command_accepted_unconfirmed",
            provider_request_id=str(request_id) if request_id else None,
            metadata={"http_status": response.status_code, "opening_confirmed": False},
        )


class MockSaltoClient:
    """Mock SALTO client."""

    is_physical: ClassVar[bool] = False

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
        return UnlockResult(
            success=True,
            detail="simulated",
            provider_request_id="mock-request",
            metadata={"simulated": True},
        )


class DisabledSaltoClient:
    """Non-actuating client used when physical door control is frozen."""

    is_physical: ClassVar[bool] = False

    async def unlock(self, *, access_token: str, site_id: str, lock_id: str) -> UnlockResult:
        return UnlockResult(success=False, detail="physical_unlock_disabled")
