"""Voice activity detection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np


class VoiceActivityDetector:
    """Sherpa-ONNX/Silero VAD with a simulation-only energy fallback."""

    def __init__(self, model_path: str | None = None, *, threshold: float = 0.5) -> None:
        self.model_path = model_path
        self.threshold = threshold
        self._model: Any | None = None
        self._load_error: str | None = None
        self._load_attempted = False
        self._continuous_speech_samples = 0
        self._max_continuous_speech_samples = 4 * 16_000

    @property
    def available(self) -> bool:
        return self._model is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def load(self) -> None:
        if self.available:
            return
        self._load_attempted = True
        try:
            if not self.model_path or not Path(self.model_path).is_file():
                raise FileNotFoundError("Silero VAD ONNX model is missing")
            import sherpa_onnx

            silero = sherpa_onnx.SileroVadModelConfig(
                model=self.model_path,
                threshold=self.threshold,
                min_silence_duration=0.15,
                min_speech_duration=0.08,
                window_size=512,
                max_speech_duration=30,
            )
            config = sherpa_onnx.VadModelConfig(
                silero_vad=silero,
                sample_rate=16000,
                num_threads=1,
                provider="cpu",
                debug=False,
            )
            self._model = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=5)
        except Exception as exc:
            self._model = None
            self._load_error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(f"Voice activity detector failed to load: {self._load_error}") from exc
        self._load_error = None

    def score(self, chunk: bytes) -> float:
        pcm = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        if pcm.size == 0:
            return 0.0

        if self._model is None:
            try:
                self.load()
            except Exception:
                pass

        if self._model is not None:
            try:
                self._model.accept_waveform(pcm / 32768.0)
                detected = self._model.is_speech_detected()
                while not self._model.empty():
                    self._model.pop()
                if detected:
                    self._continuous_speech_samples += int(pcm.size)
                    if self._continuous_speech_samples >= self._max_continuous_speech_samples:
                        # Loud continuous office noise can otherwise make sherpa's
                        # internal circular buffer grow indefinitely.
                        self._model.reset()
                        self._continuous_speech_samples = 0
                else:
                    self._continuous_speech_samples = 0
                return 1.0 if detected else 0.0
            except Exception:
                self._load_error = "RuntimeError: Silero VAD inference failed"
                self._model = None
                self._continuous_speech_samples = 0
                return 0.0

        if self._load_attempted:
            return 0.0
        rms = float(np.sqrt(np.mean(np.square(pcm / 32768.0))))
        return max(0.0, min(1.0, rms * 10.0))
