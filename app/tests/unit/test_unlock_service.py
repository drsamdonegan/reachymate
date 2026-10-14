from __future__ import annotations

import pytest

from app.services.salto.locks import MockSaltoClient
from app.services.salto.unlock_service import UnlockService


class FakeAuth:
    def __init__(self) -> None:
        self.calls = 0

    async def get_access_token(self) -> str:
        self.calls += 1
        return "token"


class PhysicalClient:
    is_physical = True

    def __init__(self) -> None:
        self.calls = 0

    async def unlock(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError("physical client must not be called")


class MemoryCommands:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def reserve(self, access_event_id: str):  # type: ignore[no-untyped-def]
        if access_event_id in self.rows:
            return self.rows[access_event_id], False
        self.rows[access_event_id] = {"status": "PENDING"}
        return self.rows[access_event_id], True

    def update(self, access_event_id: str, **kwargs):  # type: ignore[no-untyped-def]
        self.rows[access_event_id].update(kwargs)
        return self.rows[access_event_id]


@pytest.mark.asyncio
async def test_unlock_service_suppresses_duplicate_event(settings) -> None:
    client = MockSaltoClient()
    commands = MemoryCommands()
    service = UnlockService(settings=settings, auth=FakeAuth(), client=client, commands=commands)

    first = await service.unlock_for_event("event-1")
    second = await service.unlock_for_event("event-1")

    assert first.success is True
    assert second.detail == "duplicate_suppressed"
    assert len(client.calls) == 1
    assert commands.rows["event-1"]["status"] == "SIMULATED"

    restarted_service = UnlockService(
        settings=settings,
        auth=FakeAuth(),
        client=client,
        commands=commands,
    )
    after_restart = await restarted_service.unlock_for_event("event-1")
    assert after_restart.detail == "duplicate_suppressed"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_unlock_service_blocks_physical_client_outside_explicit_production(settings) -> None:
    auth = FakeAuth()
    client = PhysicalClient()
    service = UnlockService(
        settings=settings,
        auth=auth,
        client=client,
        commands=MemoryCommands(),
    )

    result = await service.unlock_for_event("event-physical")

    assert result.success is False
    assert result.detail == "physical_unlock_disabled"
    assert auth.calls == 0
    assert client.calls == 0
