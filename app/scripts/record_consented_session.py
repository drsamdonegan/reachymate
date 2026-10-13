"""Explicitly consented, bounded post-DSP corpus recording; no app/DB boot."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import wave
from pathlib import Path

from app.config import Settings
from app.services.audio.xvf import XvfController
from app.services.reachy.media import ReachyMediaAdapter
from app.services.system.telemetry import TelemetryBroker


def validate_recording_path(path: Path) -> None:
    if path.suffix != ".wav":
        raise ValueError("Recording must be a WAV")
    if path.exists() or path.with_suffix(".json").exists():
        raise ValueError("Refusing to overwrite an existing consented recording")
    # Put private corpora under the ignored local data directory. Never in docs.
    root = (Path(__file__).resolve().parents[2] / "data" / "audio-corpus").resolve()
    if not path.resolve().is_relative_to(root):
        raise ValueError("Consented recordings must be inside ignored data/audio-corpus")


async def record(args):
    if not args.consent_confirmed or not args.notice_displayed:
        raise RuntimeError("All audible participants must consent after the recording notice")
    if not 0 < args.seconds <= 300:
        raise ValueError("Recording duration must be within five minutes")
    validate_recording_path(args.output)
    settings = Settings(
        reachy_host="127.0.0.1", reachy_connection_mode="network", reachy_media_backend="lite_direct"
    )
    media = ReachyMediaAdapter(settings=settings, telemetry=TelemetryBroker())
    xvf = XvfController(profile=args.profile)
    media.xvf = xvf
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    complete = False
    try:
        await media.connect()
        await asyncio.to_thread(xvf.open)
        await media.start()
        if settings.audio_processing_enabled and media._audio_processor is None:
            raise RuntimeError("Post-DSP recording requires host audio processing")
        # O_EXCL and 0600 prevent replacing or broadly exposing a recording.
        fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as file, wave.open(file, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(settings.sample_rate_hz)
            start = time.monotonic()
            while time.monotonic() - start < args.seconds:
                # This is the exact processed PCM16 path used by AudioRouter,
                # after channel selection and residual WebRTC AEC/NS.
                target.writeframesraw(await media.read_chunk())
        complete = True
        metadata = {
            "consent_confirmed": True,
            "notice_displayed": True,
            "post_dsp": True,
            "profile": args.profile,
            "sample_rate": settings.sample_rate_hz,
            "input_channel": settings.reachy_input_channel,
            "duration_seconds": args.seconds,
            "recorded_at_unix": time.time(),
            "split": args.split,
            "category": args.category,
            "retention_days": 30,
        }
        path = args.output.with_suffix(".json")
        path.write_text(json.dumps(metadata, indent=2) + "\n")
        path.chmod(0o600)
        print(json.dumps({"recording": str(args.output.resolve()), "post_dsp": True}))
    finally:
        await media.disconnect()
        await asyncio.to_thread(xvf.close)
        if not complete:
            args.output.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--consent-confirmed", action="store_true")
    parser.add_argument("--notice-displayed", action="store_true")
    parser.add_argument("--profile", choices=["factory", "pollen_conv", "door_v1"], default="factory")
    parser.add_argument("--split", choices=["train", "validation", "test"], default="validation")
    parser.add_argument("--category", required=True)
    asyncio.run(record(parser.parse_args()))


if __name__ == "__main__":
    main()
