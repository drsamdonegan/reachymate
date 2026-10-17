"""A bounded camera still for an explicitly requested visual question."""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class ModelCameraView:
    jpeg: bytes = field(repr=False)
    width: int
    height: int
    captured_at: float
    source: str = "Reachy Mini Camera"


def capture_model_view(camera) -> ModelCameraView:
    """Read the existing camera owner; keep pixels in memory and preserve BGR colors."""

    frame = camera.get_frame()
    captured_at = time.monotonic()
    if frame is None or frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("camera_frame_unavailable")
    image = Image.fromarray(frame[:, :, ::-1])
    image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    for quality in (80, 60, 40):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality)
        jpeg = buffer.getvalue()
        if len(jpeg) <= 512 * 1024:
            return ModelCameraView(jpeg, image.width, image.height, captured_at)
    raise ValueError("camera_image_too_large")
