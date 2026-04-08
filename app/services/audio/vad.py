"""Voice activity detection."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


class VoiceActivityDetector:
    """Silero-backed VAD with an energy fallback."""

    def __init__(self) -> None:
        self._model: Any | None = None
        self._lazy_failed = False

    def score(self, chunk: bytes) -> float:
        pcm = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        if pcm.size == 0:
            return 0.0

        if self._model is None and not self._lazy_failed:
            try:
                from silero_vad import load_silero_vad

                self._model = load_silero_vad()
            except Exception:
                self._lazy_failed = True

        if self._model is not None:
            try:
                import torch

                waveform = torch.from_numpy(pcm / 32768.0)
                return float(self._model(waveform, 16000).item())
            except Exception:
                self._lazy_failed = True
                self._model = None

        rms = float(np.sqrt(np.mean(np.square(pcm / 32768.0))))
        return max(0.0, min(1.0, rms * 10.0))
