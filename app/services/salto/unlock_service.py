"""SALTO unlock orchestration with idempotency."""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.models.domain import UnlockResult
from app.services.salto.auth import SaltoOAuthManager
from app.services.salto.locks import SaltoClient


class UnlockService:
    """Call SALTO only after policy allow."""

    def __init__(
        self,
        *,
        settings: Settings,
        auth: SaltoOAuthManager,
        client: SaltoClient,
        commands,
    ) -> None:
        self.settings = settings
        self.auth = auth
        self.client = client
        self.commands = commands
        self._lock = asyncio.Lock()

    async def unlock_for_event(self, access_event_id: str) -> UnlockResult:
        is_physical = bool(getattr(self.client, "is_physical", True))
        if is_physical and not (self.settings.is_production and self.settings.physical_unlock_enabled):
            return UnlockResult(success=False, detail="physical_unlock_disabled")

        async with self._lock:
            _command, reserved = self.commands.reserve(access_event_id)
        if not reserved:
            return UnlockResult(success=False, detail="duplicate_suppressed", metadata={"suppressed": True})

        try:
            access_token = await self.auth.get_access_token()
        except asyncio.CancelledError:
            self._record(access_event_id, status="UNCERTAIN", detail="cancelled_during_auth")
            raise
        except Exception:
            result = UnlockResult(success=False, detail="token_error")
            self._record_result(access_event_id, result, status="AUTH_FAILED")
            return result

        try:
            result = await self.client.unlock(
                access_token=access_token,
                site_id=self.settings.salto_site_id,
                lock_id=self.settings.salto_lock_id,
            )
        except asyncio.CancelledError:
            self._record(access_event_id, status="UNCERTAIN", detail="cancelled_during_unlock")
            raise
        except Exception:
            result = UnlockResult(success=False, detail="unlock_error")
            self._record_result(access_event_id, result, status="FAILED")
            return result

        if result.metadata.get("simulated"):
            status = "SIMULATED"
        elif result.success:
            status = "CONFIRMED"
        elif result.detail == "unlock_command_accepted_unconfirmed":
            status = "ACCEPTED_UNCONFIRMED"
        else:
            status = "FAILED"
        self._record_result(access_event_id, result, status=status)
        return result

    def _record_result(self, access_event_id: str, result: UnlockResult, *, status: str) -> None:
        self._record(
            access_event_id,
            status=status,
            detail=result.detail,
            provider_request_id=result.provider_request_id,
            metadata=result.metadata,
        )

    def _record(self, access_event_id: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.commands.update(access_event_id, **kwargs)
