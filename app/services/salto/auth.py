"""SALTO OAuth helpers."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import Settings
from app.storage.repositories import SaltoTokenRepository


class SaltoOAuthManager:
    """Manage SALTO OAuth tokens."""

    def __init__(
        self,
        *,
        settings: Settings,
        token_repo: SaltoTokenRepository,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.token_repo = token_repo
        self.client = httpx.AsyncClient(base_url=settings.salto_auth_base_url.rstrip("/"), timeout=10.0, transport=transport)
        self._pending_states: set[str] = set()

    def build_authorize_url(self) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)
        self._pending_states.add(state)
        url = (
            f"{self.settings.salto_auth_base_url.rstrip('/')}/oauth/authorize"
            f"?response_type=code&client_id={self.settings.salto_client_id}"
            f"&redirect_uri={self.settings.salto_redirect_uri}&state={state}"
        )
        return url, state

    async def handle_callback(self, *, code: str, state: str) -> dict[str, Any]:
        if state not in self._pending_states:
            raise ValueError("Invalid OAuth state")
        self._pending_states.discard(state)
        return await self._exchange_code(code)

    async def get_access_token(self) -> str:
        token = self.token_repo.get()
        if token is None:
            raise RuntimeError("SALTO tokens have not been provisioned yet")
        expires_at = token.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at and expires_at <= datetime.now(timezone.utc) + timedelta(seconds=30):
            if not token.refresh_token:
                raise RuntimeError("SALTO access token expired and no refresh token is stored")
            await self._refresh_token(token.refresh_token)
            token = self.token_repo.get()
            if token is None:
                raise RuntimeError("Failed to persist refreshed SALTO token")
        return token.access_token

    async def close(self) -> None:
        await self.client.aclose()

    async def _exchange_code(self, code: str) -> dict[str, Any]:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.salto_redirect_uri,
            "client_id": self.settings.salto_client_id,
            "client_secret": self.settings.salto_client_secret,
        }
        response = await self.client.post("/oauth/token", data=payload)
        response.raise_for_status()
        data = response.json()
        self._save_token_response(data)
        return data

    async def _refresh_token(self, refresh_token: str) -> dict[str, Any]:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.settings.salto_client_id,
            "client_secret": self.settings.salto_client_secret,
        }
        response = await self.client.post("/oauth/token", data=payload)
        response.raise_for_status()
        data = response.json()
        self._save_token_response(data)
        return data

    def _save_token_response(self, data: dict[str, Any]) -> None:
        expires_in = int(data.get("expires_in", 3600))
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        self.token_repo.save(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=expires_at,
            token_type=data.get("token_type", "Bearer"),
        )
