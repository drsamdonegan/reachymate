"""Face embedding."""

from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np

from app.services.vision.face_detect import DetectedFace


class FaceEmbedder(Protocol):
    def embed(self, frame: np.ndarray, face: DetectedFace) -> list[float]: ...


class SFaceEmbedder:
    """OpenCV SFace embedder."""

    def __init__(self, model_path: str | None = None) -> None:
        self.model_path = model_path
        self._recognizer = None
        self._lazy_failed = False

    def embed(self, frame: np.ndarray, face: DetectedFace) -> list[float]:
        if self._recognizer is None and not self._lazy_failed:
            try:
                import cv2

                self._recognizer = cv2.FaceRecognizerSF.create(self.model_path or "", "")
            except Exception:
                self._lazy_failed = True
                self._recognizer = None

        if self._recognizer is None:
            return DummyFaceEmbedder().embed(frame, face)

        import cv2

        face_row = np.array([[*face.bbox, *(coord for pair in face.landmarks for coord in pair), face.confidence]])
        aligned = self._recognizer.alignCrop(frame, face_row[0])
        embedding = self._recognizer.feature(aligned)
        vector = embedding.flatten().astype(np.float32)
        norm = np.linalg.norm(vector)
        if norm == 0:
            return vector.tolist()
        return (vector / norm).tolist()


class DummyFaceEmbedder:
    """Deterministic fallback embedder for simulation and tests."""

    def embed(self, frame: np.ndarray, face: DetectedFace) -> list[float]:
        x, y, w, h = face.bbox
        digest = hashlib.sha256(f"{x}:{y}:{w}:{h}:{frame.shape}".encode("utf-8")).digest()
        raw = np.frombuffer(digest * 8, dtype=np.uint8)[:128].astype(np.float32)
        raw = (raw / 255.0) - 0.5
        norm = np.linalg.norm(raw)
        return (raw / norm).tolist()
