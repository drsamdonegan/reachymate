"""Fetch a backend-held KEK and unwrap a device-bound local data-key envelope."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class BackendKeyProvider:
    def __init__(self, settings, *, transport=None):
        url = urlsplit(settings.kek_endpoint or "")
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("The kiosk key endpoint requires a fixed HTTPS URL")
        if not settings.kiosk_device_credential or not settings.data_key_envelope_path:
            raise ValueError("A device credential and wrapped local data key are required")
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=3, follow_redirects=False, trust_env=False, transport=transport
        )
        self.available = False

    async def fetch(self):
        self.available = False
        try:
            headers = {"Authorization": "Bearer " + self.settings.kiosk_device_credential.get_secret_value()}
            async with self.client.stream(
                "GET",
                self.settings.kek_endpoint,
                headers=headers,
                params={"device_id": self.settings.device_id},
            ) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 4096:
                        raise ValueError("Key response exceeds limits")
            payload = json.loads(data)
            envelope_bytes = Path(self.settings.data_key_envelope_path).read_bytes()
            if len(envelope_bytes) > 4096:
                raise ValueError("Key envelope exceeds limits")
            envelope = json.loads(envelope_bytes)
            if payload["device_id"] != self.settings.device_id or payload["key_id"] != envelope["key_id"]:
                raise ValueError("Key envelope is for a different device or key")
            kek = bytearray(base64.urlsafe_b64decode(payload["kek"]))
            try:
                if len(kek) != 32:
                    raise ValueError("Invalid KEK")
                nonce = base64.urlsafe_b64decode(envelope["nonce"])
                ciphertext = base64.urlsafe_b64decode(envelope["ciphertext"])
                aad = f"kiosk_data_key|{self.settings.device_id}|{envelope['key_id']}".encode()
                key = AESGCM(bytes(kek)).decrypt(nonce, ciphertext, aad)
                if len(key) != 32:
                    raise ValueError("Invalid data key")
                self.available = True
                return base64.urlsafe_b64encode(key).decode()
            finally:
                kek[:] = b"\0" * len(kek)
        except Exception:
            return None

    async def close(self):
        self.available = False
        await self.client.aclose()
