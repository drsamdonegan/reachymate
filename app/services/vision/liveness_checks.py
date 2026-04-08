"""Passive frame quality checks."""

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
) -> FaceQuality:
    """Compute simple image-quality heuristics for a face candidate."""

    brightness = float(frame.mean()) if frame.size else 0.0
    blur_score = _variance_of_laplacian(frame)
    flags: list[FaceQualityFlag] = []
    face_size = 0

    if face_count == 0:
        flags.append(FaceQualityFlag.NO_FACE)
    if face_count > 1:
        flags.append(FaceQualityFlag.MULTIPLE_FACES)
    if bbox is not None:
        _, _, width, height = bbox
        face_size = min(width, height)
        if face_size < settings.min_face_size:
            flags.append(FaceQualityFlag.FACE_TOO_SMALL)
    if brightness < settings.min_brightness:
        flags.append(FaceQualityFlag.LOW_BRIGHTNESS)
    if blur_score < settings.min_blur_score:
        flags.append(FaceQualityFlag.BLURRY)
    if not stable:
        flags.append(FaceQualityFlag.UNSTABLE)
    if not flags:
        flags.append(FaceQualityFlag.OK)

    usable = flags == [FaceQualityFlag.OK]
    return FaceQuality(
        usable=usable,
        flags=flags,
        face_count=face_count,
        face_size=face_size,
        blur_score=blur_score,
        brightness=brightness,
    )


def _variance_of_laplacian(frame: np.ndarray) -> float:
    gray = frame.mean(axis=2) if frame.ndim == 3 else frame
    gy, gx = np.gradient(gray.astype(np.float32))
    return float(np.var(gx) + np.var(gy))
