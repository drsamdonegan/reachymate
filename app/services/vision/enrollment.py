"""Enrollment workflow."""

from __future__ import annotations

import base64
import io

import numpy as np
from PIL import Image

from app.config import Settings
from app.services.vision.face_detect import YuNetFaceDetector
from app.services.vision.face_embed import FaceEmbedder
from app.services.vision.face_match import select_best_face
from app.services.vision.liveness_checks import assess_face_quality


class EnrollmentService:
    """Create or update a local enrolled user and their embeddings."""

    def __init__(self, *, settings: Settings, detector: YuNetFaceDetector, embedder: FaceEmbedder, user_repo, face_store) -> None:
        self.settings = settings
        self.detector = detector
        self.embedder = embedder
        self.user_repo = user_repo
        self.face_store = face_store

    def enroll(
        self,
        *,
        full_name: str,
        slack_user_id: str,
        slack_display_name: str,
        company: str | None,
        samples: list[dict[str, str]],
    ) -> dict[str, object]:
        user = self.user_repo.upsert(
            full_name=full_name,
            slack_user_id=slack_user_id,
            slack_display_name=slack_display_name,
            company=company,
        )

        embeddings: list[tuple[list[float], float, str | None]] = []
        diagnostics: list[dict[str, object]] = []

        for sample in samples:
            label = sample.get("label")
            image = decode_image(sample["image_b64"])
            detections = self.detector.detect(image)
            face = select_best_face(detections, image.shape[1], image.shape[0]) if detections else None
            quality = assess_face_quality(image, face_count=len(detections), bbox=face.bbox if face else None, settings=self.settings)
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

            vector = self.embedder.embed(image, face)
            embeddings.append((vector, quality.blur_score, label))

        self.face_store.replace_embeddings(user.id, embeddings)
        return {
            "user_id": user.id,
            "full_name": user.full_name,
            "slack_user_id": user.slack_user_id,
            "embedding_count": len(embeddings),
            "samples": diagnostics,
        }


def decode_image(value: str) -> np.ndarray:
    raw = base64.b64decode(value)
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    return np.array(image)
