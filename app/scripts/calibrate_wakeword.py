"""Estimate a wakeword threshold from wav files."""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

from app.config import Settings
from app.services.audio.wakeword import WakeWordDetector


def iter_chunks(samples: np.ndarray, chunk_frames: int) -> list[bytes]:
    return [
        samples[offset : offset + chunk_frames].tobytes() for offset in range(0, len(samples), chunk_frames)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run office-noise samples through the wake detector")
    parser.add_argument("wav_files", nargs="+")
    parser.add_argument("--model-path", default=None)
    args = parser.parse_args()

    settings = Settings()
    detector = WakeWordDetector(
        args.model_path or settings.wakeword_model_path,
        engine=settings.wakeword_engine,
        vosk_model_path=settings.vosk_model_path,
        phrase=settings.wake_phrase,
        sherpa_model_dir=settings.sherpa_kws_model_dir,
        sherpa_keywords_path=settings.sherpa_kws_keywords_path,
        sherpa_threshold=settings.sherpa_kws_threshold,
        sherpa_score=settings.sherpa_kws_score,
        vosk_fallback_enabled=settings.wakeword_vosk_fallback_enabled,
    )
    scores: list[float] = []
    for wav_path in args.wav_files:
        with wave.open(str(Path(wav_path)), "rb") as wav_file:
            raw = wav_file.readframes(wav_file.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16)
        for chunk in iter_chunks(samples, chunk_frames=1280):
            scores.append(detector.score(chunk))

    if not scores:
        print("No scores collected.")
        return
    print(
        {
            "observed_max": round(max(scores), 4),
            "observed_p95": round(float(np.percentile(scores, 95)), 4),
            "suggested_threshold": round(min(0.95, float(np.percentile(scores, 99)) + 0.05), 4),
        }
    )


if __name__ == "__main__":
    main()
