"""Face embedding."""

from __future__ import annotations

import hashlib
import threading
from typing import Protocol

import numpy as np

from app.services.vision.embedding_guard import EmbeddingGuard, zeroize
from app.services.vision.face_detect import DetectedFace


class FaceEmbedder(Protocol):
    def embed(self, frame: np.ndarray, face: DetectedFace) -> list[float]: ...


class SFaceEmbedder:
    """OpenCV SFace embedder."""

    def __init__(self, model_path: str | None = None, *, allow_fallback: bool = True) -> None:
        self.model_path = model_path
        self.allow_fallback = allow_fallback
        self._recognizer = None
        self._lazy_failed = False
        self._lock = threading.RLock()
        self.model_id = "sface-2021dec"

    @property
    def model_sha256(self) -> str | None:
        from pathlib import Path

        path = Path(self.model_path or "")
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    @property
    def available(self) -> bool:
        return self._recognizer is not None

    def load(self) -> None:
        with self._lock:
            self._load()

    def _load(self) -> None:
        if self._recognizer is not None:
            return
        try:
            import cv2

            self._recognizer = cv2.FaceRecognizerSF.create(self.model_path or "", "")
            self._lazy_failed = False
        except Exception:
            self._lazy_failed = True
            self._recognizer = None
            raise

    def embed(self, frame: np.ndarray, face: DetectedFace) -> list[float]:
        with self._lock:
            return self._embed(frame, face)

    def _embed(self, frame: np.ndarray, face: DetectedFace) -> list[float]:
        EmbeddingGuard.require_scope()
        if self._recognizer is None and not self._lazy_failed:
            try:
                self.load()
            except Exception:
                if not self.allow_fallback:
                    raise

        if self._recognizer is None:
            if not self.allow_fallback:
                raise RuntimeError("SFace model is unavailable")
            return DummyFaceEmbedder().embed(frame, face)

        face_row = np.array(
            [[*face.bbox, *(coord for pair in face.landmarks for coord in pair), face.confidence]]
        )
        EmbeddingGuard.record_embedding()
        aligned = self._recognizer.alignCrop(frame, face_row[0])
        embedding = vector = None
        try:
            embedding = self._recognizer.feature(aligned)
            vector = embedding.flatten().astype(np.float32)
            norm = np.linalg.norm(vector)
            if norm == 0:
                raise RuntimeError("SFace returned an empty biometric vector")
            vector /= norm
            return vector.tolist()
        finally:
            zeroize(aligned)
            zeroize(embedding)
            zeroize(vector)


class DummyFaceEmbedder:
    """Deterministic fallback embedder for simulation and tests."""

    def embed(self, frame: np.ndarray, face: DetectedFace) -> list[float]:
        EmbeddingGuard.require_scope()
        EmbeddingGuard.record_embedding()
        x, y, w, h = face.bbox
        digest = hashlib.sha256(f"{x}:{y}:{w}:{h}:{frame.shape}".encode("utf-8")).digest()
        raw = np.frombuffer(digest * 8, dtype=np.uint8)[:128].astype(np.float32)
        raw = (raw / 255.0) - 0.5
        try:
            norm = np.linalg.norm(raw)
            raw /= norm
            return raw.tolist()
        finally:
            zeroize(raw)
