"""Launch the real Reachy camera/audio stack in a safe interactive test profile."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
from pathlib import Path

LIVE_TEST_DEFAULTS = {
    "APP_ENV": "staging",
    "HOST": "127.0.0.1",
    "PORT": "8080",
    "ALLOW_DEV_ENDPOINTS": "true",
    "REACHY_ENABLED": "true",
    "REACHY_SIMULATION": "false",
    "REACHY_HOST": "127.0.0.1",
    "REACHY_PORT": "8000",
    "REACHY_CONNECTION_MODE": "network",
    "REACHY_MEDIA_BACKEND": "lite_direct",
    "REACHY_CAMERA_DEVICE_INDEX": "1",
    "REALTIME_ENABLED": "true",
    "VOICE_API": "gpt_live",
    "TTS_ENGINE": "live",
    "OPENAI_LIVE_MODEL": "gpt-live-1",
    "OPENAI_REALTIME_MODEL": "gpt-realtime-2.1",
}


def _daemon_is_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--demo", action="store_true", help="Use loopback mock bookings and front desk; still shadow only"
    )
    parser.add_argument(
        "--no-daemon-check",
        action="store_true",
        help="Start even when the local Reachy daemon port is not reachable.",
    )
    args = parser.parse_args()

    for key, value in LIVE_TEST_DEFAULTS.items():
        os.environ.setdefault(key, value)
    # This launcher is permanently non-actuating even if a shell or .env says otherwise.
    os.environ["PHYSICAL_UNLOCK_ENABLED"] = "false"
    os.environ["HOST"] = "127.0.0.1"
    os.environ["ACCESS_MODE"] = "shadow"
    os.environ["DOOR_STAGE"] = "0"
    if args.demo:
        os.environ["FRONTDESK_URL"] = "http://127.0.0.1:8092"
        os.environ["MLAI_BACKEND_URL"] = "http://127.0.0.1:8091"
        os.environ["UNCALIBRATED_CONVERSATION_DEMO"] = "true"
        os.environ["ATTENDED_CHAT_ENABLED"] = "true"
        os.environ["CAMERA_VISION_ENABLED"] = "true"
    if shutil.disk_usage(Path.cwd()).free < 5 * 2**30:
        raise SystemExit("Live test requires at least 5 GiB free")

    from app.config import BASE_DIR, Settings

    settings = Settings()
    if not settings.data_encryption_key:
        raise SystemExit("DATA_ENCRYPTION_KEY is required before a real-robot run.")
    if settings.openai_api_key is None:
        raise SystemExit("OPENAI_API_KEY is missing. Add it to .env before starting the live test.")
    if settings.audio_processing_enabled:
        try:
            from pywebrtc_audio import AudioProcessor  # noqa: F401
        except ImportError as exc:
            raise SystemExit(
                "Echo cancellation is missing. Run `uv sync --extra audio` before the live test."
            ) from exc
    required_models = (
        settings.vad_model_path,
        settings.vosk_model_path,
        settings.sherpa_kws_model_dir,
        settings.sherpa_kws_keywords_path,
        settings.face_detector_model_path,
        settings.face_embedder_model_path,
    )
    missing = [path for path in required_models if not Path(path).exists()]
    if missing:
        raise SystemExit(
            "Required local models are missing. Run `.venv/bin/python -m app.scripts.provision_models` first."
        )
    if not args.no_daemon_check and not _daemon_is_reachable(settings.reachy_host, settings.reachy_port):
        raise SystemExit(
            "Reachy daemon is not reachable. Start it in another terminal with:\n"
            f"{BASE_DIR}/.venv/bin/reachy-mini-daemon --no-media "
            "--no-wake-up-on-start --goto-sleep-on-stop --fastapi-host 127.0.0.1 "
            "--dataset-update-interval 0 --no-preload-datasets"
        )

    import uvicorn

    from app.main import create_app

    api_label = "GPT-Live" if settings.voice_api == "gpt_live" else "Realtime"
    print(f"Reachy {api_label} voice test ({settings.voice_model}): say 'Hey Reachy', then speak normally.")
    print(f"Local diagnostics: http://{settings.host}:{settings.port}")
    print("Door action: placeholder only; no hardware actuation is possible.")
    if settings.uncalibrated_conversation_demo and not settings.camera_calibration_path:
        print("Attention: vendor intrinsics for conversation only; field calibration remains pending.")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
