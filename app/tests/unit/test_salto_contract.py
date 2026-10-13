from __future__ import annotations

import httpx
import pytest

from app.services.salto.locks import HttpSaltoClient


@pytest.mark.asyncio
async def test_salto_uses_current_locking_contract_but_does_not_claim_open(settings) -> None:
    settings.salto_api_base_url = "https://vendor-confirmed.example"
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "lock-1", "locked_state": "open"},
            headers={"x-request-id": "salto-request-1"},
        )

    client = HttpSaltoClient(settings, transport=httpx.MockTransport(handler))
    result = await client.unlock(access_token="token", site_id="site-1", lock_id="lock-1")
    await client.close()

    assert requests[0].method == "PATCH"
    assert requests[0].url.path == "/v1.2/sites/site-1/locks/lock-1/locking"
    assert requests[0].content == b'{"locked_state":"unlocked"}'
    assert result.success is False
    assert result.detail == "unlock_command_accepted_unconfirmed"
    assert result.provider_request_id == "salto-request-1"
    assert result.metadata == {"http_status": 200, "opening_confirmed": False}


@pytest.mark.asyncio
async def test_salto_failure_metadata_never_persists_provider_body(settings) -> None:
    settings.salto_api_base_url = "https://vendor-confirmed.example"
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="sensitive provider detail")

    client = HttpSaltoClient(settings, transport=httpx.MockTransport(handler))
    result = await client.unlock(access_token="token", site_id="site-1", lock_id="lock-1")
    await client.close()

    assert result.success is False
    assert result.metadata == {"http_status": 403}
    assert "sensitive" not in str(result)
