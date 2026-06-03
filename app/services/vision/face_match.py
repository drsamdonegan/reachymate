"""Face matching and identification pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import numpy as np

from app.config import Settings
from app.models.domain import FaceMatchResult
from app.services.vision.camera import CameraAdapter
from app.services.vision.face_detect import DetectedFace, YuNetFaceDetector
from app.services.vision.face_embed import FaceEmbedder
from app.services.vision.liveness_checks import assess_face_quality


class FaceMatcher:
    """Match an embedding against enrolled users."""

    def match(self, embedding: list[float], embeddings_by_user: Mapping[str, list[list[float]]]) -> tuple[str | None, float | None]:
        target = np.array(embedding, dtype=np.float32)
        best_user: str | None = None
        best_score: float | None = None
        for user_id, vectors in embeddings_by_user.items():
            for vector in vectors:
                score = float(np.dot(target, np.array(vector, dtype=np.float32)))
                if best_score is None or score > best_score:
                    best_user = user_id
                    best_score = score
        return best_user, best_score


class IdentificationService:
    """Stable face capture, embed, and match."""

    def __init__(
        self,
        *,
        settings: Settings,
        camera: CameraAdapter,
        detector: YuNetFaceDetector,
        embedder: FaceEmbedder,
        matcher: FaceMatcher,
        face_store,
    ) -> None:
        self.settings = settings
        self.camera = camera
        self.detector = detector
        self.embedder = embedder
        self.matcher = matcher
        self.face_store = face_store
        self._last_face_present = False

    def has_recent_face(self) -> bool:
        return self._last_face_present

    def probe_face_presence(self) -> bool:
        frame = self.camera.get_frame()
        if frame is None:
            self._last_face_present = False
            return False
        self._last_face_present = len(self.detector.detect(frame)) > 0
        return self._last_face_present

    async def identify(self) -> FaceMatchResult:
        samples: list[tuple[np.ndarray, DetectedFace, tuple[int, int, int, int]]] = []

        for _ in range(self.settings.face_stability_frames):
            frame = self.camera.get_frame()
            if frame is None:
                return FaceMatchResult(None, None, assess_face_quality(np.zeros((1, 1, 3)), face_count=0, bbox=None, settings=self.settings))

            detections = self.detector.detect(frame)
            self._last_face_present = len(detections) > 0
            if len(detections) == 0:
                return FaceMatchResult(None, None, assess_face_quality(frame, face_count=0, bbox=None, settings=self.settings))
            if len(detections) > 1:
                return FaceMatchResult(None, None, assess_face_quality(frame, face_count=len(detections), bbox=None, settings=self.settings))

            best_face = select_best_face(detections, frame.shape[1], frame.shape[0])
            samples.append((frame, best_face, best_face.bbox))
            await asyncio.sleep(0.05)

        stable = _is_stable([bbox for _, _, bbox in samples])
        frame, face, bbox = samples[-1]
        quality = assess_face_quality(frame, face_count=1, bbox=bbox, settings=self.settings, stable=stable)
        if not quality.usable:
            return FaceMatchResult(None, None, quality, bbox=bbox, stable_frames=len(samples))

        embedding = self.embedder.embed(frame, face)
        user_id, similarity = self.matcher.match(embedding, self.face_store.list_embeddings())
        if similarity is None or similarity < self.settings.face_match_threshold:
            user_id = None
        return FaceMatchResult(
            matched_user_id=user_id,
            similarity=similarity,
            quality=quality,
            bbox=bbox,
            stable_frames=len(samples),
            embedding=embedding,
        )


def select_best_face(detections: list[DetectedFace], frame_width: int, frame_height: int) -> DetectedFace:
    """Choose the most central and largest face."""

    center_x = frame_width / 2
    center_y = frame_height / 2

    def score(face: DetectedFace) -> tuple[float, float]:
        x, y, w, h = face.bbox
        face_center_x = x + (w / 2)
        face_center_y = y + (h / 2)
        distance = abs(face_center_x - center_x) + abs(face_center_y - center_y)
        area = w * h
        return (distance, -area)

    return sorted(detections, key=score)[0]


def _is_stable(bboxes: list[tuple[int, int, int, int]]) -> bool:
    if len(bboxes) <= 1:
        return True
    xs = [bbox[0] for bbox in bboxes]
    ys = [bbox[1] for bbox in bboxes]
    return (max(xs) - min(xs) <= 30) and (max(ys) - min(ys) <= 30)
