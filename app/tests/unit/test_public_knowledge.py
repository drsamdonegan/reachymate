from __future__ import annotations

import httpx
import pytest

from app.services.knowledge.public_client import PublicKnowledgeClient, PublicKnowledgeError


@pytest.mark.asyncio
async def test_public_healthcheck_requires_configuration_and_live_backend(settings) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ready": True})

    client = PublicKnowledgeClient(settings, transport=httpx.MockTransport(handler))
    assert await client.healthcheck() is False
    assert requests == []

    settings.mlai_organization_domain = "mlai.au"
    assert await client.healthcheck() is True
    assert requests[0].url.path == "/healthz/ready"
    await client.close()


@pytest.mark.asyncio
async def test_public_client_uses_scoped_principal_and_preserves_citations(settings) -> None:
    settings.mlai_public_knowledge_token = "public-token"
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "answered",
                "answer": "MLAI runs a public monthly meetup.",
                "warnings": [],
                "citations": [
                    {
                        "item_id": "item-1",
                        "public_key": "monthly-meetup",
                        "revision": 2,
                        "title": "Monthly meetup",
                        "published_at": "2026-08-01T00:00:00Z",
                    }
                ],
            },
        )

    client = PublicKnowledgeClient(settings, transport=httpx.MockTransport(handler))
    answer = await client.answer("When is the meetup?", request_id="req-1")
    await client.close()

    assert answer.status == "answered"
    assert answer.citations[0].public_key == "monthly-meetup"
    assert requests[0].headers["Authorization"] == "ServicePrincipal public-token"
    assert requests[0].headers["X-Request-ID"] == "req-1"
    assert "organization_domain" not in requests[0].content.decode()


@pytest.mark.asyncio
async def test_public_client_uses_domain_without_principal(settings) -> None:
    settings.mlai_organization_domain = "MLAI.AU"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert b'"organization_domain":"mlai.au"' in request.content
        return httpx.Response(
            200,
            json={
                "status": "abstained",
                "answer": "I do not have published knowledge for that.",
                "warnings": [],
                "citations": [],
            },
        )

    client = PublicKnowledgeClient(settings, transport=httpx.MockTransport(handler))
    answer = await client.answer("private plans")
    await client.close()
    assert answer.status == "abstained"


@pytest.mark.asyncio
async def test_public_client_fails_closed_on_malformed_or_oversized_input(settings) -> None:
    settings.mlai_organization_domain = "mlai.au"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "answered", "answer": "unsupported"})

    client = PublicKnowledgeClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(PublicKnowledgeError):
        await client.answer("x" * 501)
    with pytest.raises(PublicKnowledgeError):
        await client.answer("question")
    await client.close()


@pytest.mark.asyncio
async def test_public_client_hides_upstream_failure_details(settings) -> None:
    settings.mlai_organization_domain = "mlai.au"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "private internal failure"})

    client = PublicKnowledgeClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(PublicKnowledgeError, match="unavailable") as exc_info:
        await client.answer("What is MLAI?")
    await client.close()
    assert "private internal" not in str(exc_info.value)
