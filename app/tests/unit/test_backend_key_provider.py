import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services.system.key_provider import BackendKeyProvider


@pytest.mark.asyncio
async def test_key_is_bound_to_device_and_backend_is_required(settings, tmp_path):
    def encode(value):
        return base64.urlsafe_b64encode(value).decode()

    kek, data_key, nonce = b"k" * 32, b"d" * 32, b"n" * 12
    path = tmp_path / "envelope.json"
    ciphertext = AESGCM(kek).encrypt(nonce, data_key, b"kiosk_data_key|reachy-lobby|one")
    path.write_text(json.dumps({"key_id": "one", "nonce": encode(nonce), "ciphertext": encode(ciphertext)}))
    configured = settings.model_copy(
        update={
            "kek_endpoint": "https://backend.example/key",
            "data_key_envelope_path": str(path),
            "kiosk_device_credential": __import__("pydantic").SecretStr("test-device-key"),
        }
    )

    async def respond(request):
        assert request.headers["Authorization"] == "Bearer test-device-key"
        return httpx.Response(200, json={"device_id": "reachy-lobby", "key_id": "one", "kek": encode(kek)})

    provider = BackendKeyProvider(configured, transport=httpx.MockTransport(respond))
    assert await provider.fetch() == encode(data_key)
    provider.settings = configured.model_copy(update={"device_id": "stolen-device"})
    assert await provider.fetch() is None and not provider.available
    await provider.close()


def test_key_endpoint_rejects_plain_http(settings):
    with pytest.raises(ValueError, match="HTTPS"):
        BackendKeyProvider(settings.model_copy(update={"kek_endpoint": "http://backend.example/key"}))
