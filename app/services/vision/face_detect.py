"""Face detection."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class DetectedFace:
    bbox: tuple[int, int, int, int]
    confidence: float
    landmarks: list[tuple[float, float]] = field(default_factory=list)


class YuNetFaceDetector:
    """OpenCV YuNet face detector."""

    def __init__(self, model_path: str | None = None, *, strict: bool = False) -> None:
        self.model_path = model_path
        self.strict = strict
        self._detector = None
        self._lazy_failed = False
        self._lock = threading.RLock()

    @property
    def available(self) -> bool:
        return self._detector is not None

    def load(self) -> None:
        with self._lock:
            self._load()

    def _load(self) -> None:
        if self._detector is not None:
            return
        try:
            import cv2

            self._detector = cv2.FaceDetectorYN.create(self.model_path or "", "", (320, 320), 0.9, 0.3, 5000)
            self._lazy_failed = False
        except Exception:
            self._lazy_failed = True
            self._detector = None
            raise

    def detect(self, frame: np.ndarray) -> list[DetectedFace]:
        # OpenCV mutates network input size/state; tracking and access capture
        # share this detector but execute on independent CPU workers.
        with self._lock:
            return self._detect(frame)

    def _detect(self, frame: np.ndarray) -> list[DetectedFace]:
        if self._detector is None and not self._lazy_failed:
            try:
                self.load()
            except Exception:
                if self.strict:
                    raise

        if self._detector is None:
            return []

        height, width = frame.shape[:2]
        self._detector.setInputSize((width, height))
        _, faces = self._detector.detect(frame)
        if faces is None:
            return []

        detected: list[DetectedFace] = []
        for row in faces:
            x, y, w, h = [int(value) for value in row[:4]]
            landmarks = [(float(row[i]), float(row[i + 1])) for i in range(4, 14, 2)]
            detected.append(DetectedFace(bbox=(x, y, w, h), confidence=float(row[-1]), landmarks=landmarks))
        return detected
