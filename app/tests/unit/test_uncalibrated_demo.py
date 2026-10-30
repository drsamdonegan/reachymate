from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.runtime import AppContext
from app.services.vision.attention import AttentionService, CameraCalibration


def test_uncalibrated_demo_requires_shadow_staging():
    profile = dict(
        _env_file=None,
        app_env="staging",
        uncalibrated_conversation_demo=True,
        access_mode="shadow",
        door_stage=0,
    )
    assert Settings(**profile).uncalibrated_conversation_demo
    for override in (
        {"app_env": "production"},
        {"access_mode": "live"},
        {"door_stage": 1},
        {"physical_unlock_enabled": True},
        {"av_gate_mode": "pilot"},
        {"doa_bearing_validated": True},
    ):
        with pytest.raises(ValidationError):
            Settings(**(profile | override))


def test_vendor_demo_keeps_unmeasured_status_and_disables_face_motion(monkeypatch):
    vendor = CameraCalibration(np.eye(3), np.zeros(5), measured=False)
    monkeypatch.setattr(CameraCalibration, "sdk_demo", classmethod(lambda cls: vendor))
    attention = AttentionService(
        camera=None,
        detector=None,
        calibration=None,
        on_face=object(),
        uncalibrated_demo=True,
    )
    assert attention.tracker is not None
    assert attention.degraded_reason == "sdk_intrinsics_unmeasured_demo"
    assert attention.on_face is None
    assert attention.latest() is None


def test_missing_calibration_stays_closed_by_default():
    attention = AttentionService(camera=None, detector=None, calibration=None)
    attention.set_enabled(True)
    assert attention.tracker is None
    assert attention.latest() is None


def test_dashboard_preview_keeps_dormant_camera_off_and_active_capture_open():
    calls = []
    ctx = SimpleNamespace(
        reachy_media=SimpleNamespace(release_idle_camera=lambda: calls.append("release")),
        attention=SimpleNamespace(enabled=False),
        event_checkin=SimpleNamespace(scanning=False),
        luma_express=SimpleNamespace(active=False),
        camera=SimpleNamespace(get_snapshot_bytes=lambda: calls.append("capture") or b"jpeg"),
    )
    assert AppContext.get_camera_snapshot(ctx) is None
    assert calls == []
    ctx.attention.enabled = True
    assert AppContext.get_camera_snapshot(ctx) == b"jpeg"
    assert calls == ["capture"]
