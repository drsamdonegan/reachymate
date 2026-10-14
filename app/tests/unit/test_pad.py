from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest

from app.models.enums import HealthStatus
from app.services.vision.pad import ATTACK_SPECIES, PadSentinel, parallax_nose_residual


def test_parallax_rejects_flat_homography():
    base = np.array([[20.0, 20.0], [80.0, 20.0], [50.0, 50.0], [25.0, 80.0], [75.0, 80.0]])
    planar = [base + [index * 2.0, 0.0] for index in range(5)]
    assert parallax_nose_residual(planar, yaw_degrees=[-5.0, -2.5, 0.0, 2.5, 5.0]) < 1e-8
    nonplanar = [frame.copy() for frame in planar]
    for index, frame in enumerate(nonplanar):
        frame[2, 0] += index * 2.0
    assert parallax_nose_residual(nonplanar, yaw_degrees=[-5.0, -2.5, 0.0, 2.5, 5.0]) > 0.04
    with pytest.raises(ValueError, match="sweep"):
        parallax_nose_residual(planar, yaw_degrees=[0.0] * 5)


def test_pad_unavailable_without_bound_approved_attack_evidence(tmp_path):
    models = [
        SimpleNamespace(spec=SimpleNamespace(variant=name, sha256=sha))
        for name, sha in [("MiniFASNetV2", "a" * 64), ("MiniFASNetV1SE", "b" * 64)]
    ]
    ensemble = SimpleNamespace(models=models)
    sentinel = PadSentinel(ensemble, camera_unique_id="camera")
    assert sentinel.health == HealthStatus.ERROR
    assert sentinel.evaluate([], [], []).status == "UNAVAILABLE"
    evidence = {
        "approved": True,
        "held_out": True,
        "owner_approval_id": "approved-release",
        "calibration_sha256": "c" * 64,
        "camera_unique_id": "camera",
        "model_sha256": {model.spec.variant: model.spec.sha256 for model in models},
        "passive_threshold": 0.9,
        "parallax_threshold": 0.04,
        "attacks": {species: {"attempts": 300, "accepted": 0} for species in ATTACK_SPECIES},
        "bona_fide_attempts": 100,
        "bpcer": 0.02,
    }
    path = tmp_path / "evidence.json"
    raw = json.dumps(evidence).encode()
    path.write_bytes(raw)
    approved = PadSentinel(
        ensemble,
        camera_unique_id="camera",
        calibration_sha256="c" * 64,
        evidence_path=path,
        expected_evidence_sha256=hashlib.sha256(raw).hexdigest(),
        real_door=True,
        clock=lambda: 1.0,
    )
    assert approved.health == HealthStatus.HEALTHY
    for _ in range(3):
        approved.record_failure()
    assert approved.health == HealthStatus.ERROR
    invalid = PadSentinel(
        ensemble,
        camera_unique_id="different-camera",
        calibration_sha256="c" * 64,
        evidence_path=path,
        expected_evidence_sha256=hashlib.sha256(raw).hexdigest(),
        real_door=True,
    )
    assert invalid.health == HealthStatus.ERROR
