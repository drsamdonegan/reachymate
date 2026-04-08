"""SALTO unlock orchestration with idempotency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.models.domain import UnlockResult
from app.services.salto.auth import SaltoOAuthManager
from app.services.salto.locks import SaltoClient


class UnlockService:
    """Call SALTO only after policy allow."""

    def __init__(self, *, settings: Settings, auth: SaltoOAuthManager, client: SaltoClient) -> None:
        self.settings = settings
        self.auth = auth
        self.client = client
        self._recent_unlocks: dict[str, datetime] = {}

    async def unlock_for_event(self, access_event_id: str) -> UnlockResult:
        now = datetime.now(timezone.utc)
        expiry = now - timedelta(seconds=self.settings.unlock_idempotency_seconds)
        self._recent_unlocks = {
            key: value for key, value in self._recent_unlocks.items() if value >= expiry
        }
        if access_event_id in self._recent_unlocks:
            return UnlockResult(success=True, detail="duplicate_suppressed", metadata={"suppressed": True})

        try:
            access_token = await self.auth.get_access_token()
        except Exception as exc:
            return UnlockResult(success=False, detail=f"token_error:{exc}")

        result = await self.client.unlock(
            access_token=access_token,
            site_id=self.settings.salto_site_id,
            lock_id=self.settings.salto_lock_id,
        )
        if result.success:
            self._recent_unlocks[access_event_id] = now
        return result
