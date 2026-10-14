"""Offline, immutable spoken notices independent of cloud availability."""

from __future__ import annotations

import asyncio
import hashlib
import io
import wave
from pathlib import Path
from typing import Any

import numpy as np
import soxr

from app.services.system.telemetry import TelemetryBroker

CLIP_DIR = Path(__file__).resolve().parents[2] / "assets" / "clips"
CLIP_HASHES = {
    "consent_notice": "113e79290c62a74ebbfcf746e141524e228e9fdd217ad61580dc344c2c09399a",
    "cloud_unavailable": "620271305f89ed8b648d2397ae802f277ac23916a78909fc08031a680eb772b7",
    "frontdesk": "474a9c1e7b2bd1a53c85bf3a6b167d08b1698b3e1abfce6699004d8597ef94e0",
    "goodbye": "229cb248faabd8ad70a9c31574151368868b447f112103fe47a9641e7abc36c9",
}
CONSENT_NOTICE = (
    "I can check you in by comparing your face with members who've opted in; nothing is kept "
    "if you're not a match. Say 'check me in' to go ahead, or 'no thanks' for the front desk."
)


def load_clip(name: str, *, directory: Path = CLIP_DIR) -> tuple[np.ndarray, int]:
    expected = CLIP_HASHES.get(name)
    if expected is None:
        raise ValueError("Clip is outside the allowlist")
    payload = (directory / (name + ".wav")).read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError("Fixed audio clip failed integrity verification")
    with wave.open(io.BytesIO(payload), "rb") as clip:
        if clip.getnchannels() != 1 or clip.getsampwidth() != 2 or clip.getcomptype() != "NONE":
            raise ValueError("Fixed clip must be mono PCM16")
        rate = clip.getframerate()
        samples = np.frombuffer(clip.readframes(clip.getnframes()), dtype="<i2").astype(np.float32) / 32768
    if not 8000 <= rate <= 48000 or not 0 < samples.size <= rate * 30:
        raise ValueError("Fixed audio clip exceeds bounds")
    return samples, rate


class FallbackAudio:
    """Play only reviewed fixed clips, and return after the complete audio drains."""

    def __init__(
        self, *, playback: Any, telemetry: TelemetryBroker | None = None, directory: Path = CLIP_DIR
    ) -> None:
        self.playback = playback
        self.telemetry = telemetry
        self.directory = directory
        self._lock = asyncio.Lock()

    async def play(self, name: str) -> None:
        # Validate before touching the device, including when it is unplugged.
        samples, rate = await asyncio.to_thread(load_clip, name, directory=self.directory)
        async with self._lock:
            await self.playback.start_playing()
            target_rate = self.playback.output_sample_rate()
            if target_rate <= 0:
                raise RuntimeError("Offline clip output is unavailable")
            if target_rate != rate:
                samples = soxr.resample(samples, rate, target_rate).astype(np.float32)
            played_before = self.playback.output_frames_played()
            try:
                async with asyncio.timeout(samples.size / target_rate + 5):
                    # An 80-ms tap preserves AEC reference and speech-motion timing.
                    chunk_frames = max(1, int(target_rate * 0.08))
                    for offset in range(0, samples.size, chunk_frames):
                        await self.playback.push_audio(samples[offset : offset + chunk_frames])
                    while self.playback.output_active():
                        await asyncio.sleep(0.02)
                    if self.playback.output_frames_played() - played_before < samples.size:
                        raise RuntimeError("Fixed notice playback was interrupted")
            except BaseException:
                await self.playback.clear_output()
                raise
            if self.telemetry is not None:
                self.telemetry.publish("speech", {"event": "fixed_clip_played", "clip": name})
