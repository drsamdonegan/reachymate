from __future__ import annotations

import pytest

from app.services.salto.locks import MockSaltoClient
from app.services.salto.unlock_service import UnlockService


class FakeAuth:
    async def get_access_token(self) -> str:
        return "token"


@pytest.mark.asyncio
async def test_unlock_service_suppresses_duplicate_event(settings) -> None:
    client = MockSaltoClient()
    service = UnlockService(settings=settings, auth=FakeAuth(), client=client)

    first = await service.unlock_for_event("event-1")
    second = await service.unlock_for_event("event-1")

    assert first.success is True
    assert second.detail == "duplicate_suppressed"
    assert len(client.calls) == 1
