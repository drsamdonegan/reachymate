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
    """AVFoundation enumeration order used by OpenCV; reject virtual cameras."""
    if sys.platform != "darwin":
        return []
    import AVFoundation as av

    return [
        {"index": i, "name": str(device.localizedName()), "unique_id": str(device.uniqueID())}
        for i, device in enumerate(av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeVideo))
    ]


def resolve_camera(*, name: str = "Reachy Mini Camera", unique_id: str | None = None) -> int:
    matches = [
        d for d in camera_devices() if (d["unique_id"] == unique_id if unique_id else d["name"] == name)
    ]
    if len(matches) != 1 or any(
        token in matches[0]["name"].casefold() for token in ("virtual", "obs", "continuity")
    ):
        raise RuntimeError("Expected one physical Reachy camera matching name/uniqueID")
    return int(matches[0]["index"])
