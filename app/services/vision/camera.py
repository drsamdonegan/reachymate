"""Camera adapters."""

from __future__ import annotations

import io
import logging
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
        self._frame = self._build_placeholder_frame()

    async def start(self) -> None:
        if not self.settings.simulation_use_webcam:
            return
        try:
            import cv2

            self._capture = cv2.VideoCapture(self.settings.camera_device_index)
        except Exception as exc:
            logger.warning("Simulation webcam unavailable: %s", exc)
            self._capture = None

    async def stop(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def get_frame(self) -> np.ndarray | None:
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
        return np.array(image)


class ReachyCameraAdapter(SimulationCameraAdapter):
    """Reachy camera adapter."""

    def __init__(
        self,
        *,
        settings: Settings,
        telemetry: TelemetryBroker,
        robot_provider,
    ) -> None:
        super().__init__(settings=settings, telemetry=telemetry)
        self.robot_provider = robot_provider

    def get_frame(self) -> np.ndarray | None:
        robot = self.robot_provider()
        if robot is not None:
            try:
                frame = robot.media.get_frame()
                if frame is not None:
                    self._frame = frame
            except Exception:
                logger.debug("Reachy frame fetch failed", exc_info=True)
        return super().get_frame()


def encode_frame_as_jpeg(frame: np.ndarray) -> bytes:
    """Encode a numpy frame to JPEG bytes without requiring OpenCV."""

    if frame.ndim == 3 and frame.shape[2] == 3:
        image = Image.fromarray(frame.astype("uint8"))
    else:
        image = Image.fromarray(frame.astype("uint8"), mode="L")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()
