"""Read macOS media authorisation without opening devices or prompting."""

from __future__ import annotations

import sys


def permission_status() -> dict[str, str]:
    if sys.platform != "darwin":
        return {"camera": "not_applicable", "microphone": "not_applicable"}
    try:
        import AVFoundation as av

        names = {0: "not_determined", 1: "restricted", 2: "denied", 3: "authorized"}
        return {
            key: names.get(int(av.AVCaptureDevice.authorizationStatusForMediaType_(media)), "unknown")
            for key, media in (("camera", av.AVMediaTypeVideo), ("microphone", av.AVMediaTypeAudio))
        }
    except ImportError:
        return {"camera": "unavailable", "microphone": "unavailable"}


def camera_devices() -> list[dict]:
    """Match OpenCV AVFoundation's video+muxed list, sorted by uniqueID."""
    if sys.platform != "darwin":
        return []
    import AVFoundation as av

    devices = list(av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeVideo))
    devices += list(av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeMuxed))
    devices.sort(key=lambda device: str(device.uniqueID()))
    return [
        {"index": i, "name": str(device.localizedName()), "unique_id": str(device.uniqueID())}
        for i, device in enumerate(devices)
    ]


def resolve_camera(*, name: str = "Reachy Mini Camera", unique_id: str | None = None) -> int:
    return int(resolve_camera_device(name=name, unique_id=unique_id)["index"])


def resolve_camera_device(*, name: str = "Reachy Mini Camera", unique_id: str | None = None) -> dict:
    matches = [
        d for d in camera_devices() if d["name"] == name and (not unique_id or d["unique_id"] == unique_id)
    ]
    if len(matches) != 1 or any(
        token in matches[0]["name"].casefold() for token in ("virtual", "obs", "continuity")
    ):
        raise RuntimeError("Expected one physical Reachy camera matching name/uniqueID")
    return dict(matches[0])
