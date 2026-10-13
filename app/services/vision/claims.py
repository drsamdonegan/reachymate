"""A non-biometric signed QR claim; names inferred by a model cannot claim identity."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class VerifiedIdentityClaim:
    user_id: str
    expires_at: float
    nonce: str
    source: str = "signed_qr"


class IdentityClaimVerifier:
    def __init__(self, signing_key: bytes, *, clock=time.time):
        if len(signing_key) < 32:
            raise ValueError("QR claims require an independent signing key of at least 32 bytes")
        self.signing_key, self.clock = signing_key, clock
        self._used: dict[str, float] = {}

    def verify(self, token: str) -> VerifiedIdentityClaim:
        if len(token) > 2048:
            raise ValueError("Identity claim is too large")
        try:
            payload_part, signature_part = token.split(".")
            raw = base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4))
            signature = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
            expected = hmac.new(self.signing_key, payload_part.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("Invalid identity claim signature")
            payload = json.loads(raw)
            now = self.clock()
            expiry = float(payload["exp"])
            user_id, nonce = payload["user_id"], payload["nonce"]
            if (
                payload.get("purpose") != "reachy_access"
                or payload.get("source") != "signed_qr"
                or not isinstance(user_id, str)
                or not 0 < len(user_id) <= 36
                or not isinstance(nonce, str)
                or not 16 <= len(nonce) <= 128
                or not now < expiry <= now + 45
            ):
                raise ValueError("Identity claim is expired or invalid")
            self._used = {value: end for value, end in self._used.items() if end > now}
            if nonce in self._used:
                raise ValueError("Identity claim has already been used")
            self._used[nonce] = expiry
            return VerifiedIdentityClaim(user_id, expiry, nonce)
        except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid signed identity claim") from exc
