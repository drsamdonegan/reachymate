"""Wakeword detection."""

from __future__ import annotations

from typing import Any

import numpy as np


class WakeWordDetector:
    """openWakeWord-backed detector with a safe fallback."""

    def __init__(self, model_path: str | None = None) -> None:
        self.model_path = model_path
        self._model: Any | None = None
        self._lazy_failed = False

    def score(self, chunk: bytes) -> float:
        pcm = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        if pcm.size == 0:
            return 0.0

        if self._model is None and not self._lazy_failed:
            try:
                from openwakeword.model import Model

                self._model = Model(wakeword_models=[self.model_path] if self.model_path else None)
            except Exception:
                self._lazy_failed = True

        if self._model is None:
            return 0.0

        try:
            predictions = self._model.predict(pcm)
            if not predictions:
                return 0.0
            return max(float(score) for score in predictions.values())
        except Exception:
            return 0.0
