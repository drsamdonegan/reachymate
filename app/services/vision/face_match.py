"""Request-scoped, multi-frame identity scoring; probes never leave RAM."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from app.config import Settings
from app.models.domain import FaceMatchResult
from app.services.vision.camera import CameraAdapter
from app.services.vision.claims import VerifiedIdentityClaim
from app.services.vision.embedding_guard import EmbeddingGuard, zeroize
from app.services.vision.face_detect import DetectedFace, YuNetFaceDetector
from app.services.vision.face_embed import FaceEmbedder
from app.services.vision.quality import assess_face_quality


async def _owned_cpu_call(function, *args, **kwargs):
    """Wait for an owned worker before the caller erases its buffers on cancel."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        result = await worker
        zeroize(result)
        raise


class FaceMatcher:
    """A person scores the mean of their best three template cosines."""

    def score_identities(
        self, embedding, embeddings_by_user: Mapping[str, list[list[float]]]
    ) -> dict[str, float]:
        target = np.array(embedding, dtype=np.float32, copy=True)
        try:
            norm = float(np.linalg.norm(target))
            if not norm or not np.isfinite(target).all():
                raise ValueError("Invalid face probe")
            target /= norm
            scores = {}
            for user_id, vectors in embeddings_by_user.items():
                similarities = []
                for vector in vectors:
                    template = np.array(vector, dtype=np.float32, copy=True)
                    try:
                        denominator = float(np.linalg.norm(template))
                        if (
                            template.shape != target.shape
                            or not denominator
                            or not np.isfinite(template).all()
                        ):
                            raise ValueError("Invalid face template or mixed model dimensions")
                        similarities.append(float(np.dot(target, template) / denominator))
                    finally:
                        zeroize(template)
                if similarities:
                    scores[user_id] = float(np.mean(sorted(similarities, reverse=True)[:3]))
            return scores
        finally:
            zeroize(target)

    def match(self, embedding, embeddings_by_user) -> tuple[str | None, float | None]:
        scores = self.score_identities(embedding, embeddings_by_user)
        if not scores:
            return None, None
        user_id = max(scores, key=scores.get)
        return user_id, scores[user_id]


@dataclass(frozen=True)
class IdentityConsensus:
    user_id: str | None
    score: float | None
    margin: float | None
    status: str


def aggregate_identity_frames(
    frames: list[tuple[dict[str, float], float]],
    *,
    match_threshold: float = 0.48,
    unknown_threshold: float = 0.30,
    minimum_margin: float = 0.08,
    minimum_frames: int = 3,
) -> IdentityConsensus:
    if len(frames) < minimum_frames:
        return IdentityConsensus(None, None, None, "UNCERTAIN")
    if all(not scores or max(scores.values()) < unknown_threshold for scores, _ in frames):
        score = max((max(scores.values()) for scores, _ in frames if scores), default=None)
        return IdentityConsensus(None, score, None, "UNKNOWN")
    winners = [max(scores, key=scores.get) if scores else None for scores, _ in frames]
    users = set().union(*(scores for scores, _ in frames))
    total_weight = sum(max(1.0, weight) for _, weight in frames)
    aggregate = {
        user: sum(scores.get(user, -1.0) * max(1.0, weight) for scores, weight in frames) / total_weight
        for user in users
    }
    ranking = sorted(aggregate, key=aggregate.get, reverse=True)
    if not ranking:
        return IdentityConsensus(None, None, None, "UNKNOWN")
    winner = ranking[0]
    score = aggregate[winner]
    # Cosines are bounded [-1,1]; an empty competing gallery has a -1 runner-up.
    margin = score - (aggregate[ranking[1]] if len(ranking) > 1 else -1.0)
    if all(user == winner for user in winners) and score >= match_threshold and margin >= minimum_margin:
        return IdentityConsensus(winner, score, margin, "MATCH")
    return IdentityConsensus(None, score, margin, "UNCERTAIN")


class IdentificationService:
    def __init__(
        self,
        *,
        settings: Settings,
        camera: CameraAdapter,
        detector: YuNetFaceDetector,
        embedder: FaceEmbedder,
        matcher: FaceMatcher,
        face_store,
        expected_track_supplier=None,
        pose_estimator=None,
    ) -> None:
        self.settings, self.camera, self.detector = settings, camera, detector
        self.embedder, self.matcher, self.face_store = embedder, matcher, face_store
        self._last_face_present = False
        self.expected_track_supplier = expected_track_supplier
        self.pose_estimator = pose_estimator

    def _quality_settings(self, frame):
        if self.pose_estimator is None:
            return self.settings
        calibration = self.pose_estimator.calibration
        minimum = math.ceil(float(calibration.matrix[0, 0]) * frame.shape[1] / calibration.width * 0.15 / 1.1)
        return self.settings.model_copy(update={"min_face_size": minimum})

    def has_recent_face(self) -> bool:
        return self._last_face_present

    def probe_face_presence(self) -> bool:
        frame = self.camera.get_frame()
        if frame is None:
            self._last_face_present = False
            return False
        try:
            self._last_face_present = bool(self.detector.detect(frame))
            return self._last_face_present
        finally:
            zeroize(frame)

    async def identify(self, *, verified_claim: VerifiedIdentityClaim | None = None) -> FaceMatchResult:
        if EmbeddingGuard.require_scope().purpose != "access":
            raise RuntimeError("Identification requires an access scope")
        if getattr(self.settings, "identification_mode", "request_scoped") == "claim_then_verify":
            if (
                not isinstance(verified_claim, VerifiedIdentityClaim)
                or verified_claim.expires_at <= time.time()
                or verified_claim.source != "signed_qr"
            ):
                raise RuntimeError("Claim-then-verify requires a locally verified signed QR claim")
        return await self._identify_capture(verified_claim=verified_claim)

    async def _identify_capture(self, *, verified_claim=None) -> FaceMatchResult:
        gallery = self.face_store.list_embeddings(model_id=getattr(self.embedder, "model_id", None))
        if verified_claim is not None:
            for user_id in list(gallery):
                if user_id != verified_claim.user_id:
                    zeroize(gallery.pop(user_id))
        frames: list[tuple[dict[str, float], float]] = []
        bboxes = []
        track_id = None
        start = time.monotonic()
        maximum_frames = 8
        required_frames = max(3, self.settings.face_stability_frames)
        quality = assess_face_quality(np.zeros((1, 1, 3)), face_count=0, bbox=None, settings=self.settings)
        last_bbox = None
        expected = self.expected_track_supplier() if self.expected_track_supplier else None
        if self.settings.reachy_enabled and not self.settings.reachy_simulation:
            if (
                expected is None
                or self.pose_estimator is None
                or not expected.in_zone
                or time.monotonic() - expected.timestamp > 0.5
            ):
                for vectors in gallery.values():
                    zeroize(vectors)
                raise RuntimeError("Access capture requires an engaged track and validated pose calibration")
        try:
            for _ in range(maximum_frames):
                if time.monotonic() - start > 1.5:
                    break
                frame = self.camera.get_frame()
                embedding = None
                try:
                    if frame is None:
                        return FaceMatchResult(None, None, quality, match_status="UNCERTAIN")
                    detections = await _owned_cpu_call(self.detector.detect, frame)
                    self._last_face_present = bool(detections)
                    if not detections:
                        quality = assess_face_quality(frame, face_count=0, bbox=None, settings=self.settings)
                        return FaceMatchResult(None, None, quality, match_status="UNCERTAIN")
                    face = select_best_face(detections, frame.shape[1], frame.shape[0])
                    if expected is not None:
                        from app.services.vision.attention import intersection_over_union

                        face = max(
                            detections,
                            key=lambda candidate: intersection_over_union(candidate.bbox, expected.bbox),
                        )

                        latest = self.expected_track_supplier()
                        if (
                            latest is None
                            or latest.track_id != expected.track_id
                            or not latest.in_zone
                            or intersection_over_union(face.bbox, expected.bbox) < 0.5
                        ):
                            quality = assess_face_quality(
                                frame, face_count=1, bbox=face.bbox, settings=self.settings, stable=False
                            )
                            return FaceMatchResult(None, None, quality, match_status="UNCERTAIN")
                    # A face of comparable size anywhere in frame makes substitution unsafe.
                    competing = [
                        candidate
                        for candidate in detections
                        if candidate is not face and candidate.bbox[2] >= 0.6 * face.bbox[2]
                    ]
                    if competing:
                        quality = assess_face_quality(
                            frame, face_count=len(competing) + 1, bbox=face.bbox, settings=self.settings
                        )
                        return FaceMatchResult(None, None, quality, match_status="UNCERTAIN")
                    current_track = getattr(face, "track_id", None)
                    if track_id is None:
                        track_id = current_track
                    bboxes.append(face.bbox)
                    stable = _is_stable(bboxes) and (track_id is None or current_track == track_id)
                    pose = (
                        await _owned_cpu_call(self.pose_estimator.estimate, face, frame.shape)
                        if self.pose_estimator
                        else None
                    )
                    if self.pose_estimator is not None and pose is None:
                        stable = False
                    quality = await _owned_cpu_call(
                        assess_face_quality,
                        frame,
                        face_count=1,
                        bbox=face.bbox,
                        settings=self._quality_settings(frame),
                        stable=stable,
                        yaw_degrees=pose.yaw_degrees if pose else getattr(face, "yaw_degrees", 0.0),
                        pitch_degrees=pose.pitch_degrees if pose else getattr(face, "pitch_degrees", 0.0),
                    )
                    last_bbox = face.bbox
                    if not stable:
                        return FaceMatchResult(
                            None,
                            None,
                            quality,
                            bbox=last_bbox,
                            stable_frames=len(frames),
                            match_status="UNCERTAIN",
                        )
                    if quality.usable:
                        embedding = await _owned_cpu_call(self.embedder.embed, frame, face)
                        scores = await _owned_cpu_call(self.matcher.score_identities, embedding, gallery)
                        frames.append((scores, float(face.bbox[2] * face.bbox[3])))
                finally:
                    zeroize(embedding)
                    zeroize(frame)
                if len(frames) >= required_frames:
                    break
                await asyncio.sleep(0.05)
            result = aggregate_identity_frames(
                frames,
                match_threshold=self.settings.face_match_threshold,
                unknown_threshold=getattr(self.settings, "face_unknown_threshold", 0.30),
                minimum_margin=getattr(self.settings, "face_match_margin", 0.08),
                minimum_frames=required_frames,
            )
            band = (
                "high" if result.status == "MATCH" else "low" if result.status == "UNKNOWN" else "uncertain"
            )
            return FaceMatchResult(
                result.user_id,
                result.score,
                quality,
                bbox=last_bbox,
                stable_frames=len(frames),
                embedding=None,
                match_status=result.status,
                margin=result.margin,
                score_band=band,
            )
        finally:
            for vectors in gallery.values():
                zeroize(vectors)


def select_best_face(detections: list[DetectedFace], frame_width: int, frame_height: int) -> DetectedFace:
    center_x, center_y = frame_width / 2, frame_height / 2

    def score(face):
        x, y, w, h = face.bbox
        return (abs(x + w / 2 - center_x) + abs(y + h / 2 - center_y), -w * h)

    return min(detections, key=score)


def _is_stable(bboxes: list[tuple[int, int, int, int]]) -> bool:
    if len(bboxes) <= 1:
        return True
    first = bboxes[0]
    for current in bboxes[1:]:
        if abs(current[0] - first[0]) > 30 or abs(current[1] - first[1]) > 30:
            return False
        if min(current[2], first[2]) / max(current[2], first[2], 1) < 0.8:
            return False
    return True
