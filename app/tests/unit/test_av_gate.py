from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from app.services.audio.doa import DoaSample
from app.services.vision.attention import AttentionObservation, AttentionTracker, CameraCalibration
from app.services.vision.av_gate import AVGate
from app.services.vision.face_detect import DetectedFace

CHUNK = np.full(1280, 200, dtype=np.int16).tobytes()


def face(now, bearing=0):
    return AttentionObservation(1, (900, 400, 150, 150), bearing, now)


def feed(gate, now=1.0, observation=None, theta=math.pi / 2, speech=True):
    observation = face(now) if observation is None else observation
    return gate.process(
        CHUNK, vad_score=1 if speech else 0, face=observation, doa=DoaSample(theta, speech, now), now=now
    )


def test_default_closed_without_face_and_silence_cadence():
    gate = AVGate()
    result = gate.process(CHUNK, vad_score=1, now=1)
    assert not result.opened
    assert result.chunks == (bytes(len(CHUNK)),)
    assert result.reason == "no_engaged_face"


def test_gate_requires_attack_replays_preroll_and_immediate_bearing_close():
    gate = AVGate(doa_validated=True)
    assert not feed(gate).opened
    result = feed(gate, 1.08)
    assert result.opened
    assert result.chunks == (CHUNK, CHUNK)
    assert feed(gate, 1.16, theta=math.radians(65)).opened  # 25deg hysteresis
    result = feed(gate, 1.24, theta=math.radians(59))  # 31deg closes immediately
    assert not result.opened
    assert result.chunks == (bytes(len(CHUNK)),)


def test_degraded_doa_keeps_face_and_speech_requirements():
    gate = AVGate(doa_validated=False)
    feed(gate, theta=0)
    result = feed(gate, 1.08, theta=0)
    assert result.opened and result.degraded
    assert not gate.process(CHUNK, vad_score=1, face=face(1), now=4).opened


def test_pilot_missing_mouth_and_still_front_face_fail_closed():
    gate = AVGate(mode="pilot", doa_validated=True)
    assert not feed(gate).opened
    silent = replace(face(1), mouth_active=False, mouth_still_seconds=0.8)
    assert not feed(gate, observation=silent).opened
    talking = replace(face(1), mouth_active=True)
    assert not feed(gate, observation=talking).opened
    assert feed(gate, 1.08, observation=replace(talking, timestamp=1.08)).opened


def test_two_behind_dither_verdicts_latch_veto():
    gate = AVGate()
    args = dict(
        before_theta=math.pi / 2,
        after_theta=math.radians(80),
        yaw_delta=math.radians(10),
        face_silent=True,
        settled_seconds=0.7,
    )
    assert gate.dither_verdict(**args, now=1) == "behind"
    assert gate.dither_verdict(**args, now=1.1) == "behind"
    result = feed(gate, 1.2)
    assert not result.opened and result.reason == "rear_veto"
    assert gate.dither_verdict(**{**args, "after_theta": math.radians(100)}, now=7) == "front"


def test_track_change_discards_preroll():
    gate = AVGate()
    feed(gate)
    result = feed(gate, 1.08, observation=replace(face(1.08), track_id=2))
    assert not result.opened
    result = feed(gate, 1.16, observation=replace(face(1.16), track_id=2))
    assert result.chunks == (CHUNK, CHUNK)


def test_intrinsics_zone_width_and_undistortion(monkeypatch):
    import cv2

    calibration = CameraCalibration(
        np.array([[1000, 0, 960], [0, 1000, 540], [0, 0, 1]], dtype=float), np.zeros(5)
    )
    called = []

    def undistort(points, matrix, distortion):
        called.append((points, matrix, distortion))
        return np.array([[[0.5, 0.0]]])

    monkeypatch.setattr(cv2, "undistortPoints", undistort)
    assert calibration.zone_width == 100
    assert calibration.crop_width == pytest.approx(150 / 1.1)
    assert calibration.bearing((900, 450, 100, 100)) == pytest.approx(-math.atan(0.5))
    assert len(called) == 1


def test_largest_stable_face_tracks_without_switching_to_new_larger_face():
    calibration = CameraCalibration(
        np.array([[1000, 0, 960], [0, 1000, 540], [0, 0, 1]], dtype=float), np.zeros(5)
    )
    tracker = AttentionTracker(calibration)
    old = DetectedFace((900, 450, 120, 120), 1.0)
    assert tracker.update([old], now=0) is None
    engaged = tracker.update([old], now=0.6)
    assert engaged is not None
    bigger = DetectedFace((1000, 450, 200, 200), 1.0)
    assert tracker.update([old, bigger], now=0.7).track_id == engaged.track_id
    assert tracker.update([bigger], now=1).track_id == engaged.track_id


def test_optional_zero_tail_stops_appending_after_700ms():
    gate = AVGate(zero_tail_seconds=0.7)
    assert gate.process(CHUNK, vad_score=0, now=1).chunks == (bytes(len(CHUNK)),)
    assert gate.process(CHUNK, vad_score=0, now=1.69).chunks == (bytes(len(CHUNK)),)
    assert gate.process(CHUNK, vad_score=0, now=1.71).chunks == ()
