"""Face detection."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class DetectedFace:
    bbox: tuple[int, int, int, int]
    confidence: float
    landmarks: list[tuple[float, float]] = field(default_factory=list)


class YuNetFaceDetector:
    """OpenCV YuNet face detector."""

    def __init__(self, model_path: str | None = None) -> None:
        self.model_path = model_path
        self._detector = None
        self._lazy_failed = False

    def detect(self, frame: np.ndarray) -> list[DetectedFace]:
        if self._detector is None and not self._lazy_failed:
            try:
                import cv2

                model_path = self.model_path or ""
                self._detector = cv2.FaceDetectorYN.create(model_path, "", (320, 320), 0.9, 0.3, 5000)
            except Exception:
                self._lazy_failed = True
                self._detector = None

        if self._detector is None:
            return []

        import cv2

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
