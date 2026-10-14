"""Crop and pose quality. These checks are deliberately not PAD."""

from __future__ import annotations

import numpy as np

from app.config import Settings
from app.models.domain import FaceQuality
from app.models.enums import FaceQualityFlag


def assess_face_quality(
    frame: np.ndarray,
    *,
    face_count: int,
    bbox: tuple[int, int, int, int] | None,
    settings: Settings,
    stable: bool = True,
    yaw_degrees: float = 0.0,
    pitch_degrees: float = 0.0,
) -> FaceQuality:
    flags: list[FaceQualityFlag] = []
    crop = frame
    face_size = 0
    if bbox is not None:
        x, y, width, height = bbox
        face_size = width
        crop = frame[max(0, y) : max(0, y + height), max(0, x) : max(0, x + width)]
        if width < settings.min_face_size or not crop.size:
            flags.append(FaceQualityFlag.FACE_TOO_SMALL)
    gray = _gray(crop)
    brightness = float(gray.mean()) if gray.size else 0.0
    blur_score = _variance_of_laplacian(crop)
    if face_count == 0:
        flags.append(FaceQualityFlag.NO_FACE)
    if face_count > 1:
        flags.append(FaceQualityFlag.MULTIPLE_FACES)
    if brightness < settings.min_brightness:
        flags.append(FaceQualityFlag.LOW_BRIGHTNESS)
    if brightness > getattr(settings, "max_brightness", 200.0):
        flags.append(FaceQualityFlag.HIGH_BRIGHTNESS)
    if blur_score < settings.min_blur_score:
        flags.append(FaceQualityFlag.BLURRY)
    if abs(yaw_degrees) > 25 or abs(pitch_degrees) > 25:
        flags.append(FaceQualityFlag.POSE)
    if not stable:
        flags.append(FaceQualityFlag.UNSTABLE)
    if not flags:
        flags.append(FaceQualityFlag.OK)
    return FaceQuality(
        usable=flags == [FaceQualityFlag.OK],
        flags=flags,
        face_count=face_count,
        face_size=face_size,
        blur_score=blur_score,
        brightness=brightness,
    )


def _gray(frame):
    if frame.ndim == 3:
        return frame[..., 0] * 0.114 + frame[..., 1] * 0.587 + frame[..., 2] * 0.299
    return frame.astype(float)


def _variance_of_laplacian(frame: np.ndarray) -> float:
    gray = _gray(frame).astype(np.float32)
    if min(gray.shape[:2], default=0) < 3:
        return 0.0
    try:
        import cv2

        return float(cv2.Laplacian(gray, cv2.CV_32F).var())
    except ImportError:
        center = gray[1:-1, 1:-1]
        laplacian = gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:] - 4 * center
        return float(laplacian.var())
