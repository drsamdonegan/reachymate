"""Deferred SALTO KS adapter. Fixed site/lock; preflight and never automatic retry."""

from __future__ import annotations

from urllib.parse import quote, urlsplit

import httpx

from app.services.door.base import ReleaseResult, ReleaseStatus


class SaltoKSProvider:
    def __init__(
        self,
        *,
        api_base: str,
        site_id: str,
        lock_id: str,
        auth,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(api_base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("SALTO API base must be the vendor-confirmed HTTPS endpoint")
        if not site_id or not lock_id:
            raise ValueError("Fixed site and lock bindings are required")
        self.auth = auth
        self.path = f"/v1.2/sites/{quote(site_id, safe='')}/locks/{quote(lock_id, safe='')}"
        self.client = httpx.AsyncClient(
            base_url=api_base.rstrip("/"), timeout=3, transport=transport, follow_redirects=False
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def release(self) -> ReleaseResult:
        try:
            token = await self.auth.get_access_token()
            headers = {"Authorization": "Bearer " + token}
            preflight = await self.client.get(self.path, headers=headers)
            if preflight.status_code != 200:
                return ReleaseResult(ReleaseStatus.REJECTED, "salto_lock_preflight_failed")
            lock = preflight.json()
            if not isinstance(lock, dict):
                return ReleaseResult(ReleaseStatus.REJECTED, "salto_lock_preflight_invalid")
            # An unknown OTP or remote-opening configuration is refused too.
            if lock.get("otp_enabled") is not False:
                return ReleaseResult(ReleaseStatus.REJECTED, "salto_otp_enabled_or_unknown")
            if lock.get("online") is False:
                return ReleaseResult(ReleaseStatus.REJECTED, "salto_lock_offline")
        except Exception:
            return ReleaseResult(ReleaseStatus.FAILED, "salto_auth_or_preflight_failed")
        try:
            response = await self.client.patch(
                self.path + "/locking", headers=headers, json={"locked_state": "unlocked"}
            )
        except httpx.ConnectError:
            return ReleaseResult(ReleaseStatus.FAILED, "salto_connection_not_established")
        except Exception:
            return ReleaseResult(ReleaseStatus.UNCERTAIN, "salto_send_outcome_uncertain")
        if 400 <= response.status_code < 500:
            return ReleaseResult(ReleaseStatus.REJECTED, "salto_release_rejected")
        if response.status_code != 200:
            return ReleaseResult(ReleaseStatus.UNCERTAIN, "salto_release_outcome_uncertain")
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Response is not an object")
        except Exception:
            return ReleaseResult(ReleaseStatus.UNCERTAIN, "salto_response_invalid")
        error_code = payload.get("ErrorCode", payload.get("error_code"))
        safe_code = str(error_code) if isinstance(error_code, (str, int)) else "none"
        if not safe_code.replace("_", "").replace("-", "").isalnum() or len(safe_code) > 40:
            safe_code = "invalid"
        if error_code not in (None, 0, "0", "None", "NoError"):
            return ReleaseResult(ReleaseStatus.REJECTED, "salto_error_code_" + safe_code)
        request_id = response.headers.get("x-request-id")
        return ReleaseResult(
            ReleaseStatus.ACCEPTED_UNCONFIRMED, "salto_accepted_error_code_" + safe_code, request_id
        )
