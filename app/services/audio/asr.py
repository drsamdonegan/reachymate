"""Tiny command recognizer."""

from __future__ import annotations

import json
from typing import Any

from app.models.enums import CommandIntent


COMMAND_GRAMMAR = [intent.value for intent in CommandIntent] + ["[unk]"]


class CommandRecognizer:
    """Vosk-backed command recognizer for a tiny grammar."""

    def __init__(self, model_path: str) -> None:
        self.model_path = model_path
        self._model: Any | None = None
        self._recognizer: Any | None = None
        self._lazy_failed = False

    def recognize(self, chunk: bytes) -> CommandIntent | None:
        if self._model is None and not self._lazy_failed:
            try:
                from vosk import KaldiRecognizer, Model

                self._model = Model(self.model_path)
                self._recognizer = KaldiRecognizer(self._model, 16000, json.dumps(COMMAND_GRAMMAR))
            except Exception:
                self._lazy_failed = True

        if self._recognizer is None:
            return None

        try:
            if self._recognizer.AcceptWaveform(chunk):
                result = json.loads(self._recognizer.Result())
            else:
                result = json.loads(self._recognizer.PartialResult())
            text = (result.get("text") or result.get("partial") or "").strip().lower()
        except Exception:
            return None

        for intent in CommandIntent:
            if text == intent.value:
                return intent
        return None
