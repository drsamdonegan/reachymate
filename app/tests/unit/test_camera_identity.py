from types import SimpleNamespace

import pytest

from app.services.system import tcc


def test_camera_index_matches_opencv_unique_id_sort_and_includes_muxed(monkeypatch):
    def device(name, uid):
        return SimpleNamespace(localizedName=lambda: name, uniqueID=lambda: uid)

    built_in = device("FaceTime HD Camera", "1FD4B3A2")
    reachy = device("Reachy Mini Camera", "0x114000038fb1002")
    muxed = device("Other physical camera", "0x0001")
    av = SimpleNamespace(
        AVMediaTypeVideo="video",
        AVMediaTypeMuxed="muxed",
        AVCaptureDevice=SimpleNamespace(
            devicesWithMediaType_=lambda kind: [built_in, reachy] if kind == "video" else [muxed]
        ),
    )
    import sys

    monkeypatch.setattr(tcc.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AVFoundation", av)
    assert tcc.resolve_camera() == 1  # Reachy, after muxed; FaceTime is index 2.
    assert tcc.camera_devices()[2]["name"] == "FaceTime HD Camera"
    assert tcc.resolve_camera_device()["unique_id"] == "0x114000038fb1002"


def test_camera_pin_never_falls_back_to_builtin_or_reused_index(monkeypatch):
    monkeypatch.setattr(
        tcc,
        "camera_devices",
        lambda: [{"index": 0, "name": "FaceTime HD Camera", "unique_id": "old-reachy-id"}],
    )
    with pytest.raises(RuntimeError, match="physical Reachy camera"):
        tcc.resolve_camera(unique_id="old-reachy-id")
