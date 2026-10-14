"""Authenticated application-layer encryption for sensitive local records."""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"


class SensitiveDataCipher:
    def __init__(self, encoded_key: Any | None) -> None:
        secret = _secret_value(encoded_key)
        self._key: bytes | None = None
        if secret:
            try:
                key = base64.urlsafe_b64decode(secret.encode("ascii"))
            except Exception as exc:
                raise ValueError("DATA_ENCRYPTION_KEY must be URL-safe base64") from exc
            if len(key) != 32:
                raise ValueError("DATA_ENCRYPTION_KEY must decode to exactly 32 bytes")
            self._key = key

    @property
    def enabled(self) -> bool:
        return self._key is not None

    def encrypt_json(self, value: Any, *, purpose: str) -> Any:
        if self._key is None:
            return value
        plaintext = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return self._encrypt(plaintext, purpose=purpose)

    def decrypt_json(self, value: Any, *, purpose: str, allow_plaintext: bool) -> Any:
        if isinstance(value, str) and value.startswith(PREFIX):
            plaintext = self._decrypt(value, purpose=purpose)
            return json.loads(plaintext)
        if allow_plaintext:
            return value
        raise RuntimeError("Unencrypted sensitive record is forbidden in production")

    def encrypt_text(self, value: str | None, *, purpose: str) -> str | None:
        if value is None or self._key is None:
            return value
        return self._encrypt(value.encode("utf-8"), purpose=purpose)

    def decrypt_text(
        self,
        value: str | None,
        *,
        purpose: str,
        allow_plaintext: bool,
    ) -> str | None:
        if value is None:
            return None
        if value.startswith(PREFIX):
            return self._decrypt(value, purpose=purpose).decode("utf-8")
        if allow_plaintext:
            return value
        raise RuntimeError("Unencrypted sensitive record is forbidden in production")

    def _encrypt(self, plaintext: bytes, *, purpose: str) -> str:
        if self._key is None:  # pragma: no cover - guarded by public methods
            raise RuntimeError("Sensitive data encryption is not configured")
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext, purpose.encode("utf-8"))
        return PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def _decrypt(self, value: str, *, purpose: str) -> bytes:
        if self._key is None:
            raise RuntimeError("Sensitive data encryption key is unavailable")
        try:
            payload = base64.urlsafe_b64decode(value[len(PREFIX) :].encode("ascii"))
            return AESGCM(self._key).decrypt(
                payload[:12],
                payload[12:],
                purpose.encode("utf-8"),
            )
        except Exception as exc:
            raise RuntimeError("Sensitive data could not be decrypted") from exc


def _secret_value(value: Any | None) -> str:
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if getter is not None else value).strip()
