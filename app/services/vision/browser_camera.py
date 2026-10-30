"""On-demand stills from Chrome's Reachy camera stream, without a second USB owner."""

from __future__ import annotations

import asyncio
import io
import re
import secrets
import time

from PIL import Image

from app.services.vision.model_view import ModelCameraView

MAX_JPEG_BYTES = 512 * 1024


def decode_browser_view(content: bytes) -> ModelCameraView:
    if not content or len(content) > MAX_JPEG_BYTES:
        raise ValueError("Invalid camera image size")
    with Image.open(io.BytesIO(content)) as image:
        if image.format != "JPEG" or not (1 <= image.width <= 1280 and 1 <= image.height <= 1280):
            raise ValueError("Expected a bounded camera JPEG")
        # Re-encode pixels only; discard metadata and never write camera frames to disk.
        image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=80)
        jpeg = buffer.getvalue()
        if len(jpeg) > MAX_JPEG_BYTES:
            raise ValueError("Camera image too large")
        return ModelCameraView(jpeg, image.width, image.height, time.monotonic())


class BrowserCameraBridge:
    """One opted-in dashboard, a short lease, and at most one requested frame."""

    LEASE_SECONDS = 15

    def __init__(self):
        self._token = None
        self._seen_at = 0.0
        self._pending = None
        self._request_id = None
        self._requested_at = 0.0

    @property
    def active(self) -> bool:
        return self._token is not None and time.monotonic() - self._seen_at < self.LEASE_SECONDS

    def start(self, label: str) -> str:
        if not re.search(r"reachy.*camera", label, re.IGNORECASE):
            raise ValueError("Select Reachy Mini Camera; other cameras are not accepted")
        if self.active:
            raise ValueError("Camera sharing is already enabled in another dashboard tab")
        self.stop()
        self._token = secrets.token_urlsafe(32)
        self._seen_at = time.monotonic()
        return self._token

    def authorize(self, token: str) -> bool:
        return bool(self.active and token and secrets.compare_digest(token, self._token))

    def poll(self, token: str) -> dict:
        if not self.authorize(token):
            raise ValueError("Camera sharing ended; enable it again")
        self._seen_at = time.monotonic()
        return {"request_id": self._request_id}

    async def capture(self) -> ModelCameraView | None:
        if not self.active or self._pending is not None:
            return None
        pending = self._pending = asyncio.get_running_loop().create_future()
        self._request_id = secrets.token_urlsafe(16)
        self._requested_at = time.monotonic()
        try:
            return await asyncio.wait_for(pending, 3.5)
        finally:
            if self._pending is pending:
                self._pending = self._request_id = None

    async def submit(self, token: str, request_id: str, content: bytes) -> None:
        pending = self._pending
        if (
            not self.authorize(token)
            or request_id != self._request_id
            or pending is None
            or pending.done()
            or time.monotonic() - self._requested_at > 3.5
        ):
            raise ValueError("Camera request expired or cancelled")
        view = await asyncio.to_thread(decode_browser_view, content)
        if not self.authorize(token) or self._pending is not pending or pending.done():
            raise ValueError("Camera request expired or cancelled")
        pending.set_result(view)

    def stop(self) -> None:
        self._token = self._request_id = None
        if self._pending is not None and not self._pending.done():
            self._pending.set_result(None)
        self._pending = None

    def diagnostics(self) -> dict:
        return {"connected": self.active, "source": "Reachy Mini Camera", "mode": "on_request"}
