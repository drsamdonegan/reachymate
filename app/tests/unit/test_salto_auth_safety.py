from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.services.salto.auth import SaltoOAuthManager


class Tokens:
    def __init__(self):
        self.token = SimpleNamespace(
            access_token="expired",
            refresh_token="previous-refresh",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )

    def get(self):
        return self.token

    def save(self, **kwargs):
        self.token = SimpleNamespace(**kwargs)


@pytest.mark.asyncio
async def test_pkce_offline_scope_and_refresh_token_retained(settings):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})

    manager = SaltoOAuthManager(
        settings=settings, token_repo=Tokens(), transport=httpx.MockTransport(handler)
    )
    url, _ = manager.build_authorize_url()
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).path == "/connect/authorize"
    assert query["code_challenge_method"] == ["S256"]
    assert "offline_access" in query["scope"][0]
    assert await manager.get_access_token() == "fresh"
    assert requests[0].url.path == "/connect/token"
    assert manager.token_repo.get().refresh_token == "previous-refresh"
    await manager.close()


@pytest.mark.asyncio
async def test_password_grant_requires_dedicated_site_admin_attestation(settings):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "new", "expires_in": 3600})

    settings.salto_username = "dedicated-user"
    settings.salto_password = "secret-password"
    settings.salto_client_id = "client"
    settings.salto_client_secret = "secret-client"
    manager = SaltoOAuthManager(
        settings=settings, token_repo=Tokens(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ValueError):
        await manager.provision_password_grant()
    assert requests == []
    await manager.provision_password_grant(dedicated_site_admin_confirmed=True)
    body = parse_qs(requests[0].content.decode())
    assert body["scope"] == ["user_api.full_access offline_access"]
    assert body["grant_type"] == ["password"]
    assert requests[0].headers["authorization"].startswith("Basic ")
    assert "client_secret" not in body
    await manager.close()


@pytest.mark.asyncio
async def test_salto_preflight_otp_prevents_patch_and_timeout_is_uncertain():
    from app.services.door.base import ReleaseStatus
    from app.services.door.salto_ks import SaltoKSProvider

    calls = []
    otp = True

    class Auth:
        async def get_access_token(self):
            return "token"

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"otp_enabled": otp})
        assert request.content == b'{"locked_state":"unlocked"}'
        raise httpx.ReadTimeout("never persist sensitive provider detail")

    provider = SaltoKSProvider(
        api_base="https://vendor-confirmed.example",
        site_id="site",
        lock_id="lock",
        auth=Auth(),
        transport=httpx.MockTransport(handler),
    )
    assert (await provider.release()).status == ReleaseStatus.REJECTED
    assert [r.method for r in calls] == ["GET"]
    otp = False
    assert (await provider.release()).status == ReleaseStatus.UNCERTAIN
    assert [r.method for r in calls] == ["GET", "GET", "PATCH"]
    await provider.close()


@pytest.mark.asyncio
async def test_pkce_state_is_single_use_and_exchange_includes_verifier(settings):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"access_token": "token", "refresh_token": "new-refresh"})

    manager = SaltoOAuthManager(
        settings=settings, token_repo=Tokens(), transport=httpx.MockTransport(handler)
    )
    url, state = manager.build_authorize_url()
    await manager.handle_callback(code="code", state=state)
    body = parse_qs(requests[0].content.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert len(body["code_verifier"][0]) >= 43
    with pytest.raises(ValueError):
        await manager.handle_callback(code="code", state=state)
    assert len(requests) == 1
    await manager.close()
