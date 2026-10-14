"""Detection-only engagement tracking; images stay in the calling process."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.services.system.heartbeat import Heartbeat
from app.services.system.supervisor import SupervisedTask


@dataclass(frozen=True)
class CameraCalibration:
    matrix: np.ndarray
    distortion: np.ndarray
    width: int = 1920
    height: int = 1080

    @classmethod
    def load(cls, path: str) -> "CameraCalibration":
        value = json.loads(Path(path).read_text())
        if not value.get("validated", False):
            raise ValueError("Camera calibration requires measured/validated intrinsics")
        matrix = np.asarray(value["K"], dtype=float).reshape(3, 3)
        distortion = np.asarray(value["D"], dtype=float)
        if (
            not np.isfinite(matrix).all()
            or not np.isfinite(distortion).all()
            or min(matrix[0, 0], matrix[1, 1]) <= 0
        ):
            raise ValueError("Invalid camera calibration")
        return cls(matrix, distortion, int(value.get("width", 1920)), int(value.get("height", 1080)))

    @property
    def zone_width(self) -> float:
        return float(self.matrix[0, 0]) * 0.15 / 1.5

    @property
    def crop_width(self) -> float:
        return float(self.matrix[0, 0]) * 0.15 / 1.1

    def direction(self, bbox: tuple) -> np.ndarray:
        import cv2

        x, y, w, h = bbox
        centre = np.array([[[x + w / 2, y + h / 2]]], dtype=np.float64)
        normal = cv2.undistortPoints(centre, self.matrix, self.distortion)[0, 0]
        return np.array([1.0, -float(normal[0]), -float(normal[1])])

    def bearing(self, bbox: tuple) -> float:
        direction = self.direction(bbox)
        return math.atan2(float(direction[1]), float(direction[0]))


@dataclass(frozen=True)
class AttentionObservation:
    track_id: int
    bbox: tuple
    bearing: float | None
    timestamp: float
    in_zone: bool = True
    mouth_active: bool | None = None
    mouth_still_seconds: float = 0.0


def intersection_over_union(a: tuple, b: tuple) -> float:
    x = max(a[0], b[0])
    y = max(a[1], b[1])
    right = min(a[0] + a[2], b[0] + b[2])
    bottom = min(a[1] + a[3], b[1] + b[3])
    intersection = max(0, right - x) * max(0, bottom - y)
    union = a[2] * a[3] + b[2] * b[3] - intersection
    return intersection / union if union > 0 else 0.0


class AttentionTracker:
    def __init__(self, calibration: CameraCalibration) -> None:
        self.calibration = calibration
        self._tracks: dict[int, tuple[tuple, float, float]] = {}
        self._next = 0
        self._engaged: int | None = None
        self.observation: AttentionObservation | None = None

    def update(self, faces, *, now: float | None = None) -> AttentionObservation | None:
        now = time.monotonic() if now is None else now
        available = set(self._tracks)
        updated: dict[int, tuple[tuple, float, float]] = {}
        for face in sorted(faces, key=lambda f: f.bbox[2] * f.bbox[3], reverse=True):
            best = max(
                available,
                key=lambda key: intersection_over_union(self._tracks[key][0], face.bbox),
                default=None,
            )
            if best is not None and intersection_over_union(self._tracks[best][0], face.bbox) >= 0.3:
                first = self._tracks[best][1]
                available.remove(best)
            else:
                self._next += 1
                best, first = self._next, now
            updated[best] = (face.bbox, first, now)
        for key in available:
            if now - self._tracks[key][2] <= 2:
                updated[key] = self._tracks[key]
        self._tracks = updated
        if self._engaged in updated:
            bbox, _first, seen = updated[self._engaged]
            if now - seen <= 1.5:
                bearing = self.calibration.bearing(bbox)
                self.observation = AttentionObservation(
                    self._engaged,
                    bbox,
                    bearing,
                    seen,
                    abs(bearing) <= math.radians(35) and bbox[2] >= self.calibration.zone_width,
                )
                return self.observation
        self._engaged = None
        candidates = [
            (key, value)
            for key, value in updated.items()
            if now - value[2] < 0.2
            and now - value[1] >= 0.5
            and value[0][2] >= self.calibration.zone_width
            and abs(self.calibration.bearing(value[0])) <= math.radians(35)
        ]
        if candidates:
            self._engaged, (bbox, _first, seen) = max(
                candidates, key=lambda item: item[1][0][2] * item[1][0][3]
            )
            self.observation = AttentionObservation(self._engaged, bbox, self.calibration.bearing(bbox), seen)
        elif self.observation is not None and now - self.observation.timestamp > 2:
            self.observation = None
        return self.observation

    def reset(self) -> None:
        self._tracks.clear()
        self._engaged = None
        self.observation = None


class AttentionService:
    def __init__(
        self, *, camera, detector, calibration: CameraCalibration | None, on_face=None, mouth=None
    ) -> None:
        self.camera, self.detector, self.on_face, self.mouth = camera, detector, on_face, mouth
        self.tracker = AttentionTracker(calibration) if calibration else None
        self.heartbeat = Heartbeat(0.1)
        self.worker = SupervisedTask("face-attention", self._run, heartbeat=self.heartbeat)
        self.enabled = False
        self.degraded_reason = None if calibration else "camera_calibration_unmeasured"

    def latest(self) -> AttentionObservation | None:
        return self.tracker.observation if self.enabled and self.tracker else None

    async def start(self) -> None:
        await self.worker.start()

    async def stop(self) -> None:
        await self.worker.stop()
        self.set_enabled(False)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled and self.tracker:
            self.tracker.reset()

    async def _run(self) -> None:
        while True:
            if self.enabled and self.tracker:
                observation = await asyncio.to_thread(self._detect)
                if observation is not None and self.on_face is not None:
                    await self.on_face(observation.bbox)
                self.heartbeat.beat()
            await asyncio.sleep(0.1)

    def _detect(self):
        import cv2

        frame = self.camera.get_frame()
        if frame is None:
            raise RuntimeError("camera_frame_unavailable")
        height, width = frame.shape[:2]
        if (width, height) != (self.tracker.calibration.width, self.tracker.calibration.height):
            raise RuntimeError("camera_calibration_resolution_mismatch")
        faces = self.detector.detect(cv2.resize(frame, (640, 360)))
        for face in faces:
            x, y, w, h = face.bbox
            face.bbox = (
                int(x * width / 640),
                int(y * height / 360),
                int(w * width / 640),
                int(h * height / 360),
            )
        observation = self.tracker.update(faces)
        if observation and self.mouth:
            from dataclasses import replace

            active, still = self.mouth.process(frame, observation.bbox)
            observation = replace(observation, mouth_active=active, mouth_still_seconds=still)
            self.tracker.observation = observation
        return observation
