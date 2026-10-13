"""Local PAD primitives with a separately approved, model-bound evidence sentinel.

Inference preprocessing follows the upstream MiniFASNet ONNX interface:
https://github.com/yakhyo/face-anti-spoofing/blob/main/onnx_inference.py
No model is downloaded, approved or marked healthy by this module.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.models.enums import HealthStatus
from app.services.vision.embedding_guard import zeroize

ATTACK_SPECIES = {"print", "cutout_print", "phone", "tablet", "laptop"}


@dataclass(frozen=True)
class PadModelSpec:
    path: Path
    sha256: str
    variant: str
    scale: float


class MiniFasNetModel:
    def __init__(self, spec: PadModelSpec):
        if spec.variant not in {"MiniFASNetV2", "MiniFASNetV1SE"}:
            raise ValueError("Unapproved PAD model variant")
        expected_scale = 2.7 if spec.variant == "MiniFASNetV2" else 4.0
        if spec.scale != expected_scale or len(spec.sha256) != 64:
            raise ValueError("PAD requires pinned model hash and trained crop scale")
        if hashlib.sha256(spec.path.read_bytes()).hexdigest() != spec.sha256:
            raise ValueError("PAD model checksum mismatch")
        import onnxruntime as ort

        self.spec = spec
        self.session = ort.InferenceSession(str(spec.path), providers=["CPUExecutionProvider"])
        inputs = self.session.get_inputs()
        if len(inputs) != 1 or inputs[0].shape[1:] != [3, 80, 80]:
            raise ValueError("Unexpected MiniFASNet input shape")
        self.input_name = inputs[0].name

    def score(self, frame: np.ndarray, bbox: tuple[int, int, int, int]) -> float:
        import cv2

        x, y, w, h = bbox
        height, width = frame.shape[:2]
        if min(w, h) <= 0:
            raise ValueError("Invalid PAD face crop")
        scale = min(self.spec.scale, (height - 1) / h, (width - 1) / w)
        sw, sh = w * scale, h * scale
        # Shift a crop at the edge to preserve the trained crop extent.
        left, top = (
            max(0.0, min(x + w / 2 - sw / 2, width - 1 - sw)),
            max(0.0, min(y + h / 2 - sh / 2, height - 1 - sh)),
        )
        crop = cv2.resize(frame[int(top) : int(top + sh) + 1, int(left) : int(left + sw) + 1], (80, 80))
        tensor = crop.astype(np.float32).transpose(2, 0, 1)[None, ...].copy()
        output = None
        try:
            output = np.asarray(self.session.run(None, {self.input_name: tensor})[0], dtype=float)
            if output.shape != (1, 3) or not np.isfinite(output).all():
                raise RuntimeError("PAD model returned invalid logits")
            probabilities = np.exp(output - output.max())
            probabilities /= probabilities.sum()
            return float(probabilities[0, 1])
        finally:
            zeroize(crop)
            zeroize(tensor)
            zeroize(output)


class PassivePadEnsemble:
    def __init__(self, models):
        if {model.spec.variant for model in models} != {"MiniFASNetV2", "MiniFASNetV1SE"} or len(models) != 2:
            raise ValueError("PAD requires both pinned MiniFASNet variants")
        self.models = models

    def score(self, frames: list[tuple[np.ndarray, tuple]]) -> float:
        if len(frames) < 5:
            raise ValueError("PAD requires at least five frames")
        scores = [model.score(frame, bbox) for frame, bbox in frames for model in self.models]
        return float(np.mean(scores))


def parallax_nose_residual(landmarks: list[np.ndarray], *, yaw_degrees: list[float]) -> float:
    """A homography fitted to eyes/mouth predicts a flat object's nose."""
    import cv2

    if len(landmarks) < 5 or len(yaw_degrees) != len(landmarks) or max(yaw_degrees) - min(yaw_degrees) < 9.0:
        raise ValueError("PAD parallax requires at least five measured poses over a ±5 degree yaw sweep")
    points = [np.asarray(frame, dtype=np.float64) for frame in landmarks]
    if any(frame.shape != (5, 2) or not np.isfinite(frame).all() for frame in points):
        raise ValueError("PAD parallax landmarks are missing")
    reference = points[0]
    interocular = float(np.linalg.norm(reference[0] - reference[1]))
    if interocular <= 0:
        raise ValueError("Invalid PAD eye landmarks")
    plane = [0, 1, 3, 4]
    residuals = []
    for current in points[1:]:
        homography, _ = cv2.findHomography(reference[plane], current[plane], method=0)
        if homography is None:
            raise ValueError("PAD homography could not be fitted")
        predicted = cv2.perspectiveTransform(reference[2:3].reshape(1, 1, 2), homography)[0, 0]
        residuals.append(float(np.linalg.norm(predicted - current[2]) / interocular))
    return float(np.mean(residuals))


@dataclass(frozen=True)
class PadResult:
    passed: bool
    status: str
    passive_score: float | None = None
    parallax_residual: float | None = None
    challenge_direction: str | None = None
    challenge_nonce: str | None = None


class PadSentinel:
    """Approval evidence must bind every active model and the pinned camera.

    expected_evidence_sha256 is supplied from the approved release record, never
    computed from arbitrary unapproved input to make the sentinel pass.
    """

    def __init__(
        self,
        ensemble: PassivePadEnsemble,
        *,
        camera_unique_id: str,
        calibration_sha256: str | None = None,
        evidence_path: Path | None = None,
        expected_evidence_sha256: str | None = None,
        real_door: bool = False,
        alert=None,
        clock=time.monotonic,
    ):
        self.ensemble, self.camera_unique_id = ensemble, camera_unique_id
        self.approved = False
        self.clock, self.alert = clock, alert
        self.failures = deque()
        self.cooldown_until = 0.0
        self.challenge = None
        if evidence_path is not None and expected_evidence_sha256:
            raw = evidence_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_evidence_sha256:
                raise ValueError("PAD evidence checksum does not match the approved release record")
            evidence = json.loads(raw)
            pinned = {model.spec.variant: model.spec.sha256 for model in ensemble.models}
            attacks = evidence.get("attacks", {})
            minimum = 300 if real_door else 100
            valid = (
                evidence.get("approved") is True
                and evidence.get("held_out") is True
                and bool(evidence.get("owner_approval_id"))
                and bool(calibration_sha256)
                and evidence.get("calibration_sha256") == calibration_sha256
                and evidence.get("camera_unique_id") == camera_unique_id
                and bool(camera_unique_id)
                and evidence.get("model_sha256") == pinned
                and evidence.get("passive_threshold") == 0.90
                and evidence.get("parallax_threshold") == 0.04
                and set(attacks) >= ATTACK_SPECIES
                and all(
                    attacks[species].get("attempts", 0) >= minimum and attacks[species].get("accepted") == 0
                    for species in ATTACK_SPECIES
                )
                and evidence.get("bona_fide_attempts", 0) >= 100
                and evidence.get("bpcer", 1.0) <= (0.03 if real_door else 0.05)
            )
            self.approved = bool(valid)

    @property
    def health(self) -> HealthStatus:
        return (
            HealthStatus.HEALTHY
            if self.approved and self.clock() >= self.cooldown_until
            else HealthStatus.ERROR
        )

    def record_failure(self):
        now = self.clock()
        self.failures.append(now)
        while self.failures and self.failures[0] < now - 600:
            self.failures.popleft()
        if len(self.failures) > 2:
            self.cooldown_until = now + 300
            if self.alert:
                self.alert("pad_failure_cooldown")

    def evaluate(
        self,
        frames,
        landmarks,
        yaw_degrees,
        *,
        challenge_nonce: str | None = None,
        challenge_yaw_degrees: float | None = None,
    ) -> PadResult:
        if self.health != HealthStatus.HEALTHY:
            return PadResult(False, "UNAVAILABLE")
        try:
            passive = self.ensemble.score(frames)
            residual = parallax_nose_residual(landmarks, yaw_degrees=yaw_degrees)
        except Exception:
            self.record_failure()
            return PadResult(False, "ERROR")
        if passive < 0.6 or residual < 0.04:
            self.record_failure()
            return PadResult(False, "REJECTED", passive, residual)
        if passive < 0.9:
            now = self.clock()
            pending = self.challenge
            if pending and now > pending[2]:
                self.challenge = None
                self.record_failure()
                return PadResult(False, "CHALLENGE_EXPIRED", passive, residual)
            if (
                pending
                and challenge_nonce == pending[0]
                and now <= pending[2]
                and challenge_yaw_degrees is not None
            ):
                sign = -1 if pending[1] == "left" else 1
                self.challenge = None
                if sign * challenge_yaw_degrees >= 15:
                    return PadResult(True, "PASSED_CHALLENGE", passive, residual)
                self.record_failure()
                return PadResult(False, "REJECTED", passive, residual)
            if pending:
                return PadResult(False, "CHALLENGE_REQUIRED", passive, residual, pending[1], pending[0])
            nonce, direction = secrets.token_urlsafe(24), secrets.choice(["left", "right"])
            self.challenge = (nonce, direction, now + 8.0)
            return PadResult(False, "CHALLENGE_REQUIRED", passive, residual, direction, nonce)
        self.challenge = None
        return PadResult(True, "PASSED", passive, residual)
