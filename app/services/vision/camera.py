"""Camera adapters."""

from __future__ import annotations

import asyncio
import io
import logging
import platform
import threading
from typing import Any, Protocol

import numpy as np
from PIL import Image, ImageDraw

from app.config import Settings
from app.services.system.telemetry import TelemetryBroker

logger = logging.getLogger(__name__)


class CameraAdapter(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def get_frame(self) -> np.ndarray | None: ...
    def get_snapshot_bytes(self) -> bytes | None: ...


class SimulationCameraAdapter:
    """Fake camera with optional webcam support."""

    def __init__(self, *, settings: Settings, telemetry: TelemetryBroker) -> None:
        self.settings = settings
        self.telemetry = telemetry
        self._capture: Any | None = None
        self._capture_lock = threading.RLock()
        self.external_owner = False
        self._frame = self._build_placeholder_frame()

    async def start(self) -> None:
        if self.external_owner or not self.settings.simulation_use_webcam:
            return
        try:
            import cv2

            self._capture = cv2.VideoCapture(self.settings.camera_device_index)
        except Exception as exc:
            logger.warning("Simulation webcam unavailable: %s", exc)
            self._capture = None

    async def stop(self) -> None:
        await asyncio.to_thread(self._stop_capture)

    def _stop_capture(self) -> None:
        with self._capture_lock:
            self._release_capture()

    def _release_capture(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def set_external_owner(self, enabled: bool) -> None:
        # Wait for any in-flight frame read before releasing the device. All callers
        # (preview, detection, model snapshots, identification) share this guard.
        with self._capture_lock:
            self.external_owner = enabled
            if enabled:
                self._release_capture()

    def get_frame(self) -> np.ndarray | None:
        with self._capture_lock:
            if self.external_owner:
                return None
            if self._capture is not None:
                ok, frame = self._capture.read()
                if ok:
                    self._frame = frame
            return None if self._frame is None else self._frame.copy()

    def get_snapshot_bytes(self) -> bytes | None:
        frame = self.get_frame()
        if frame is None:
            return None
        return encode_frame_as_jpeg(frame)

    def _build_placeholder_frame(self) -> np.ndarray:
        image = Image.new("RGB", (640, 480), "#132a3b")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((165, 95, 475, 385), radius=20, outline="#8de1ff", width=4)
        draw.text((195, 205), "Simulation Camera", fill="#e9fbff")
        return np.array(image)[:, :, ::-1].copy()


class ReachyCameraAdapter(SimulationCameraAdapter):
    """Reachy camera adapter."""

    def __init__(
        self,
        *,
        settings: Settings,
        telemetry: TelemetryBroker,
        media,
    ) -> None:
        super().__init__(settings=settings, telemetry=telemetry)
        self.media = media

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def _release_capture(self) -> None:
        self.media.release_idle_camera()

    def get_frame(self) -> np.ndarray | None:
        with self._capture_lock:
            if self.external_owner:
                return None
            try:
                frame = self.media.get_frame()
            except Exception:
                logger.debug("Reachy frame fetch failed", exc_info=True)
                return None
            return None if frame is None else frame.copy()


class PinnedLocalCameraAdapter:
    """Camera-only enrollment capture, with no SDK or motor connection."""

    def __init__(self, *, settings: Settings, telemetry: TelemetryBroker):
        self.settings, self.telemetry = settings, telemetry
        self._capture = None

    async def start(self):
        import cv2

        if platform.system() == "Darwin":
            from app.services.system.tcc import resolve_camera

            index = resolve_camera(
                name=self.settings.reachy_camera_name, unique_id=self.settings.reachy_camera_unique_id
            )
        else:
            raise RuntimeError("Camera-only identity pinning currently requires AVFoundation on macOS")
        capture = await asyncio.to_thread(cv2.VideoCapture, index)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError("Reachy camera is unavailable; check Camera permission")
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        self._capture = capture

    async def stop(self):
        if self._capture is not None:
            await asyncio.to_thread(self._capture.release)
            self._capture = None

    def get_frame(self):
        if self._capture is None:
            raise RuntimeError("Enrollment camera has not been started")
        ok, frame = self._capture.read()
        return frame if ok else None

    def get_snapshot_bytes(self):
        frame = self.get_frame()
        return None if frame is None else encode_frame_as_jpeg(frame)


def encode_frame_as_jpeg(frame: np.ndarray) -> bytes:
    """Encode a numpy frame to JPEG bytes without requiring OpenCV."""

    if frame.ndim == 3 and frame.shape[2] == 3:
        image = Image.fromarray(frame[:, :, ::-1].astype("uint8"))
    else:
        image = Image.fromarray(frame.astype("uint8"), mode="L")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()
