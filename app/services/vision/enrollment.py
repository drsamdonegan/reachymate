"""Validate all samples before one atomic, recorded-consent enrollment."""

from __future__ import annotations

import base64
import binascii
import io
import math
from uuid import uuid4

import numpy as np
from PIL import Image, ImageOps

from app.config import Settings
from app.services.vision.embedding_guard import AccessAttemptScope, zeroize
from app.services.vision.face_detect import YuNetFaceDetector
from app.services.vision.face_embed import FaceEmbedder
from app.services.vision.face_match import select_best_face
from app.services.vision.quality import assess_face_quality


class EnrollmentService:
    def __init__(
        self,
        *,
        settings: Settings,
        detector: YuNetFaceDetector,
        embedder: FaceEmbedder,
        user_repo,
        face_store,
    ) -> None:
        self.settings, self.detector, self.embedder = settings, detector, embedder
        self.user_repo, self.face_store = user_repo, face_store
        self.pose_estimator = None

    def enroll(
        self,
        *,
        full_name: str,
        slack_user_id: str,
        slack_display_name: str,
        company: str | None,
        biometric_consent: bool,
        samples: list[dict[str, str]],
        operator_id: str,
        notice_version: str,
        notice_sha256: str,
        consent_method: str = "member_checkbox",
        greet_by_name: bool = False,
        reinstate: bool = False,
        capture_source: str = "file",
    ) -> dict[str, object]:
        if not biometric_consent:
            raise ValueError("Recorded biometric consent is required before enrollment")
        if not samples or len(samples) > 20:
            raise ValueError("Enrollment requires between 1 and 20 samples")
        if self.settings.reachy_enabled and not self.settings.reachy_simulation and len(samples) < 15:
            raise ValueError("Real enrollment requires five poses with three samples each")
        if (
            self.settings.reachy_enabled
            and not self.settings.reachy_simulation
            and self.pose_estimator is None
        ):
            raise ValueError("Real enrollment requires measured camera calibration for pose/crop gates")
        if not operator_id.strip() or not notice_version.strip():
            raise ValueError("Consent operator and notice version are required")
        existing = self.user_repo.get_by_slack_id(slack_user_id)
        if existing is not None and existing.access_revoked and not reinstate:
            raise ValueError("Member access is revoked; explicit --reinstate is required")
        embeddings, diagnostics = [], []
        try:
            with AccessAttemptScope(str(uuid4()), purpose="enrollment"):
                for sample in samples:
                    image = decode_image(sample["image_b64"])
                    try:
                        detections = self.detector.detect(image)
                        face = (
                            select_best_face(detections, image.shape[1], image.shape[0])
                            if detections
                            else None
                        )
                        pose = (
                            self.pose_estimator.estimate(face, image.shape)
                            if face and self.pose_estimator
                            else None
                        )
                        quality_settings = self.settings
                        if self.pose_estimator:
                            calibration = self.pose_estimator.calibration
                            minimum = math.ceil(
                                float(calibration.matrix[0, 0])
                                * image.shape[1]
                                / calibration.width
                                * 0.15
                                / 1.1
                            )
                            quality_settings = self.settings.model_copy(update={"min_face_size": minimum})
                        quality = assess_face_quality(
                            image,
                            face_count=len(detections),
                            bbox=face.bbox if face else None,
                            settings=quality_settings,
                            stable=self.pose_estimator is None or pose is not None,
                            yaw_degrees=pose.yaw_degrees if pose else getattr(face, "yaw_degrees", 0.0),
                            pitch_degrees=pose.pitch_degrees if pose else getattr(face, "pitch_degrees", 0.0),
                        )
                        label = sample.get("label")
                        diagnostics.append(
                            {
                                "label": label,
                                "usable": quality.usable,
                                "flags": [flag.value for flag in quality.flags],
                                "face_count": quality.face_count,
                            }
                        )
                        if not quality.usable or face is None:
                            raise ValueError(f"Enrollment sample '{label or 'sample'}' is not usable")
                        embeddings.append((self.embedder.embed(image, face), quality.blur_score, label))
                    finally:
                        zeroize(image)
            user = self.face_store.enroll_atomic(
                full_name=full_name,
                slack_user_id=slack_user_id,
                slack_display_name=slack_display_name,
                company=company,
                embeddings=embeddings,
                operator_id=operator_id,
                notice_version=notice_version,
                notice_sha256=notice_sha256,
                consent_method=consent_method,
                model_id=getattr(self.embedder, "model_id", "simulation"),
                model_sha256=getattr(self.embedder, "model_sha256", None),
                capture_source=capture_source,
                greet_by_name=greet_by_name,
                reinstate=reinstate,
            )
            return {
                "user_id": user.id,
                "full_name": user.full_name,
                "slack_user_id": user.slack_user_id,
                "embedding_count": len(embeddings),
                "samples": diagnostics,
            }
        finally:
            for vector, _, _ in embeddings:
                zeroize(vector)


def decode_image(
    value: str, *, max_bytes: int = 10 * 1024 * 1024, max_pixels: int = 12_000_000
) -> np.ndarray:
    if len(value) > ((max_bytes + 2) // 3) * 4:
        raise ValueError("Enrollment image exceeds the size limit")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Enrollment image is not valid base64") from exc
    if len(raw) > max_bytes:
        raise ValueError("Enrollment image exceeds the size limit")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > max_pixels or min(image.size) < 1:
                raise ValueError("Enrollment image exceeds the pixel size limit")
            # Transpose EXIF before decoding; all OpenCV paths consume BGR.
            decoded = np.asarray(ImageOps.exif_transpose(image).convert("RGB"))
            return decoded[..., ::-1].copy()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Enrollment image cannot be decoded safely") from exc
