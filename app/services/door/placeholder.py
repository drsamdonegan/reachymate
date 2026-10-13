"""Non-actuating placeholder for the deferred physical door integration."""

from __future__ import annotations

from app.models.domain import UnlockResult


class PlaceholderDoorAction:
    """Record that entry would be granted without touching any door hardware."""

    is_placeholder = True
    actuates_hardware = False

    def __init__(self, *, commands) -> None:
        self.commands = commands

    async def record_shadow(self, access_event_id: str) -> UnlockResult:
        _command, reserved = self.commands.reserve(access_event_id)
        if not reserved:
            return UnlockResult(success=False, detail="duplicate_shadow_suppressed")
        result = UnlockResult(
            success=False,
            detail="shadow_would_allow",
            metadata={"placeholder": True, "actuated": False, "pad_absent": True, "status": "SHADOW"},
        )
        self.commands.update(access_event_id, status="SHADOW", detail=result.detail, metadata=result.metadata)
        return result

    async def execute_for_event(self, access_event_id: str) -> UnlockResult:
        """Durably record one no-op result for an allowed access attempt."""

        _command, reserved = self.commands.reserve(access_event_id)
        if not reserved:
            return UnlockResult(
                success=False,
                detail="duplicate_placeholder_suppressed",
                metadata={"placeholder": True, "actuated": False, "suppressed": True},
            )

        result = UnlockResult(
            success=True,
            detail="placeholder_would_open",
            provider_request_id=f"placeholder:{access_event_id}",
            metadata={"placeholder": True, "actuated": False},
        )
        self.commands.update(
            access_event_id,
            status="PLACEHOLDER",
            detail=result.detail,
            provider_request_id=result.provider_request_id,
            metadata=result.metadata,
        )
        return result
