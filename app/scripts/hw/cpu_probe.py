"""Offline model timing on synthetic input. No hardware, identity or media saved."""

from __future__ import annotations

import time

import numpy as np

from app.config import Settings
from app.scripts.hw.common import parser, summary, write_report
from app.services.audio.asr import CommandRecognizer
from app.services.audio.vad import VoiceActivityDetector
from app.services.audio.wakeword import WakeWordDetector
from app.services.vision.embedding_guard import AccessAttemptScope, zeroize
from app.services.vision.face_detect import DetectedFace, YuNetFaceDetector
from app.services.vision.face_embed import SFaceEmbedder


def timed(function, iterations: int):
    results = []
    for _ in range(iterations):
        start = time.perf_counter()
        value = function()
        results.append((time.perf_counter() - start) * 1000)
        zeroize(value)
    return summary(results)


def main():
    cli = parser("cpu_probe")
    cli.add_argument("--iterations", type=int, default=20)
    args = cli.parse_args()
    if not 1 <= args.iterations <= 100:
        raise ValueError("Iterations must be within1..100")
    settings = Settings(reachy_enabled=False)
    report = {
        "status": "measured",
        "input": "synthetic blanks/silence; inference timing only",
        "timings_ms": {},
    }
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    crop = np.full((112, 112, 3), 127, dtype=np.uint8)
    chunk = bytes(2560)
    detector = YuNetFaceDetector(settings.face_detector_model_path, strict=True)
    embedder = SFaceEmbedder(settings.face_embedder_model_path, allow_fallback=False)
    try:
        detector.load()
        embedder.load()
        report["timings_ms"]["yunet_640x360"] = timed(lambda: detector.detect(frame), args.iterations)
        face = DetectedFace((0, 0, 112, 112), 1, [(32, 38), (80, 38), (56, 60), (40, 82), (72, 82)])
        with AccessAttemptScope("synthetic_cpu_probe", purpose="evaluation"):
            report["timings_ms"]["sface"] = timed(lambda: embedder.embed(crop, face), args.iterations)
        rng = np.random.default_rng(17)
        probe = rng.standard_normal(128).astype(np.float32)
        probe /= np.linalg.norm(probe)
        for count in (100, 1000):
            gallery = rng.standard_normal((count, 128)).astype(np.float32)
            gallery /= np.linalg.norm(gallery, axis=1)[:, None]
            report["timings_ms"][f"cosine_gallery_{count}"] = timed(lambda: gallery @ probe, args.iterations)
            zeroize(gallery)
        zeroize(probe)
        vad = VoiceActivityDetector(settings.vad_model_path)
        vad.load()
        wake = WakeWordDetector(
            engine=settings.wakeword_engine,
            sherpa_model_dir=settings.sherpa_kws_model_dir,
            sherpa_keywords_path=settings.sherpa_kws_keywords_path,
            vosk_fallback_enabled=False,
        )
        wake.load()
        command = CommandRecognizer(settings.vosk_model_path)
        command.load()
        for name, function in (
            ("silero", lambda: vad.score(chunk)),
            ("sherpa", lambda: wake.score(chunk)),
            ("vosk", lambda: command.recognize(chunk)),
        ):
            report["timings_ms"][name] = timed(function, args.iterations)
    except Exception as exc:
        report.update({"status": "failed", "error": type(exc).__name__})
    finally:
        zeroize(frame)
        zeroize(crop)
        write_report(args.output, report)


if __name__ == "__main__":
    main()
