"""Five-landmark pose estimate for quality gates, using validated camera K/D.

A generic face geometry is a quality heuristic, not identity or liveness proof.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.services.vision.face_detect import DetectedFace


@dataclass(frozen=True)
class FacePose:
    yaw_degrees: float
    pitch_degrees: float
    reprojection_error_px: float


class FacePoseEstimator:
    def __init__(self, calibration):
        self.calibration = calibration
        self.geometry = np.array(
            [
                [-32.0, -30.0, 0.0],
                [32.0, -30.0, 0.0],
                [0.0, 0.0, -30.0],
                [-25.0, 30.0, 0.0],
                [25.0, 30.0, 0.0],
            ],
            dtype=np.float64,
        )

    def estimate(self, face: DetectedFace, frame_shape) -> FacePose | None:
        import cv2

        if len(face.landmarks) != 5:
            return None
        points = np.asarray(face.landmarks, dtype=np.float64)
        eyes = points[:2][np.argsort(points[:2, 0])]
        mouth = points[3:5][np.argsort(points[3:5, 0])]
        points = np.concatenate([eyes, points[2:3], mouth])
        height, width = frame_shape[:2]
        matrix = self.calibration.matrix.copy()
        matrix[0] *= width / self.calibration.width
        matrix[1] *= height / self.calibration.height
        ok, rotation, translation = cv2.solvePnP(
            self.geometry, points, matrix, self.calibration.distortion, flags=cv2.SOLVEPNP_EPNP
        )
        if not ok:
            return None
        ok, rotation, translation = cv2.solvePnP(
            self.geometry,
            points,
            matrix,
            self.calibration.distortion,
            rotation,
            translation,
            True,
            flags=cv2.SOLVEPNP_ITERATIVE,
        )
        if not ok or float(translation[2, 0]) <= 0:
            return None
        projected, _ = cv2.projectPoints(
            self.geometry, rotation, translation, matrix, self.calibration.distortion
        )
        residual = float(np.sqrt(np.mean(np.sum((projected.reshape(-1, 2) - points) ** 2, axis=1))))
        if not np.isfinite(residual) or residual > face.bbox[2] * 0.08:
            return None
        matrix_rotation, _ = cv2.Rodrigues(rotation)
        pitch, yaw, _ = cv2.RQDecomp3x3(matrix_rotation)[0]
        return FacePose(float(yaw), float(pitch), residual)
