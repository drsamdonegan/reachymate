"""Compatibility import. Crop quality is not presentation-attack detection."""

from app.services.vision.quality import assess_face_quality

__all__ = ["assess_face_quality"]
