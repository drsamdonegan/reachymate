"""Helpers for turning face detections into robot attention cues."""

from __future__ import annotations

from app.models.domain import BoundingBox


def face_is_nearby(bbox: BoundingBox | None, min_size: int) -> bool:
    """Return whether a face bbox is large enough to count as nearby."""

    if bbox is None:
        return False
    _, _, width, height = bbox
    return min(width, height) >= min_size
