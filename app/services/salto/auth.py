"""SALTO KS OAuth: current identity endpoint, PKCE and durable refresh retention."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from app.config import Settings
from app.storage.repositories import SaltoTokenRepository

SCOPE = "user_api.full_access offline_access"


class SaltoOAuthManager:
    def __init__(
        self,
        *,
        settings: Settings,
        token_repo: SaltoTokenRepository,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.token_repo = token_repo
        parsed = urlsplit(settings.salto_auth_base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("SALTO identity host must be configured as HTTPS")
        self.client = httpx.AsyncClient(
            base_url=settings.salto_auth_base_url.rstrip("/"),
            timeout=10,
            transport=transport,
            follow_redirects=False,
        )
        self._pending_states: dict[str, tuple[str, float]] = {}
        self._refresh_lock = asyncio.Lock()

    def build_authorize_url(self) -> tuple[str, str]:
        now = time.monotonic()
        self._pending_states = {k: v for k, v in self._pending_states.items() if v[1] > now}
        if len(self._pending_states) >= 10:
            raise ValueError("Too many pending OAuth sessions")
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self._pending_states[state] = (verifier, now + 600)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.salto_client_id,
                "redirect_uri": self.settings.salto_redirect_uri,
                "scope": SCOPE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return self.settings.salto_auth_base_url.rstrip("/") + "/connect/authorize?" + query, state

    async def handle_callback(self, *, code: str, state: str) -> dict[str, Any]:
        pending = self._pending_states.pop(state, None)
        if pending is None or pending[1] <= time.monotonic() or not code or len(code) > 4096:
            raise ValueError("Invalid or expired OAuth state")
        return await self._request_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.salto_redirect_uri,
                "code_verifier": pending[0],
            }
        )

    async def provision_password_grant(
        self, *, dedicated_site_admin_confirmed: bool = False
    ) -> dict[str, Any]:
        """Explicit provisioning only, using a dedicated vendor-approved system user."""

        if not dedicated_site_admin_confirmed:
            raise ValueError("A dedicated SALTO site_admin user must be confirmed")
        if not all(
            (
                self.settings.salto_username,
                self.settings.salto_password,
                self.settings.salto_client_id,
                self.settings.salto_client_secret,
            )
        ):
            raise ValueError("SALTO dedicated-user credentials are missing")
        return await self._request_token(
            {
                "grant_type": "password",
                "username": self.settings.salto_username,
                "password": self.settings.salto_password,
                "scope": SCOPE,
            }
        )

    async def get_access_token(self) -> str:
        async with self._refresh_lock:
            token = self.token_repo.get()
            if token is None:
                raise RuntimeError("SALTO tokens have not been provisioned")
            expires = token.expires_at
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires is not None and expires <= datetime.now(timezone.utc) + timedelta(seconds=30):
                if not token.refresh_token:
                    raise RuntimeError("SALTO token expired without a refresh token")
                await self._request_token(
                    {"grant_type": "refresh_token", "refresh_token": token.refresh_token, "scope": SCOPE},
                    previous_refresh_token=token.refresh_token,
                )
                token = self.token_repo.get()
                if token is None:
                    raise RuntimeError("SALTO refreshed token was not persisted")
            return token.access_token

    async def close(self) -> None:
        self._pending_states.clear()
        await self.client.aclose()

    async def _exchange_code(self, code: str) -> dict[str, Any]:
        # Bare authorization codes cannot bypass state and PKCE validation.
        raise ValueError("Use handle_callback with the original OAuth state")

    async def _refresh_token(self, refresh_token: str) -> dict[str, Any]:
        return await self._request_token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": SCOPE},
            previous_refresh_token=refresh_token,
        )

    async def _request_token(
        self, payload: dict[str, Any], *, previous_refresh_token: str | None = None
    ) -> dict[str, Any]:
        payload = {**payload, "client_id": self.settings.salto_client_id}
        authentication = None
        if self.settings.salto_client_secret:
            authentication = httpx.BasicAuth(self.settings.salto_client_id, self.settings.salto_client_secret)
        response = await self.client.post("/connect/token", data=payload, auth=authentication)
        if response.status_code != 200:
            raise RuntimeError("SALTO token endpoint rejected the grant")
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Token response is not an object")
            self._save_token_response(data, previous_refresh_token=previous_refresh_token)
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("SALTO token response is invalid") from exc
        return data

    def _save_token_response(
        self, data: dict[str, Any], *, previous_refresh_token: str | None = None
    ) -> None:
        access = data["access_token"]
        if not isinstance(access, str) or not access or len(access) > 32_000:
            raise ValueError("Invalid access token")
        expires_in = int(data.get("expires_in", 3600))
        if not 0 < expires_in <= 86400 or data.get("token_type", "Bearer").lower() != "bearer":
            raise ValueError("Invalid token lifetime or type")
        refresh = data.get("refresh_token") or previous_refresh_token
        if not refresh:
            old = self.token_repo.get()
            refresh = old.refresh_token if old is not None else None
        if refresh is not None and (not isinstance(refresh, str) or len(refresh) > 32_000):
            raise ValueError("Invalid refresh token")
        self.token_repo.save(
            access_token=access,
            refresh_token=refresh,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type="Bearer",
        )
