"""Tiny command recognizer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models.enums import CommandIntent

COMMAND_PHRASES = {
    "open door": CommandIntent.OPEN_DOOR,
    "open the door": CommandIntent.OPEN_DOOR,
    "check me in": CommandIntent.CONSENT_CONFIRM,
    "no thanks": CommandIntent.CONSENT_DECLINE,
    "help": CommandIntent.HELP,
    "book now": CommandIntent.BOOK_NOW,
    "repeat": CommandIntent.REPEAT,
    "cancel": CommandIntent.CANCEL,
    "stop": CommandIntent.CANCEL,
    "goodbye": CommandIntent.CANCEL,
    "go to sleep": CommandIntent.CANCEL,
}
COMMAND_GRAMMAR = [*COMMAND_PHRASES, "[unk]"]
GRAMMAR_TOKENS = {token for phrase in COMMAND_PHRASES for token in phrase.split()}


class CommandRecognizer:
    """Vosk-backed command recognizer for a tiny grammar."""

    def __init__(self, model_path: str) -> None:
        self.model_path = model_path
        self._model: Any | None = None
        self._recognizer: Any | None = None
        self._load_error: str | None = None
        self._last_partial_intent: CommandIntent | None = None

    @property
    def available(self) -> bool:
        return self._recognizer is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def load(self) -> None:
        if self.available:
            return
        try:
            if not Path(self.model_path).is_dir():
                raise FileNotFoundError("Vosk command model is missing")
            from vosk import KaldiRecognizer

            from app.services.audio.vosk_model import load_vosk_model

            self._model = load_vosk_model(self.model_path)
            self._recognizer = KaldiRecognizer(self._model, 16000, json.dumps(COMMAND_GRAMMAR))
        except Exception as exc:
            self._model = None
            self._recognizer = None
            self._load_error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(f"Command recognizer failed to load: {self._load_error}") from exc
        self._load_error = None

    def recognize(self, chunk: bytes) -> CommandIntent | None:
        if not self.available:
            try:
                self.load()
            except Exception:
                return None

        if self._recognizer is None:
            return None

        try:
            final = self._recognizer.AcceptWaveform(chunk)
            if final:
                result = json.loads(self._recognizer.Result())
            else:
                result = json.loads(self._recognizer.PartialResult())
            raw_text = (result.get("text") or result.get("partial") or "").strip().lower()
            tokens = raw_text.split()
            text = " ".join(token for token in tokens if token != "[unk]")
        except Exception:
            return None

        matched = COMMAND_PHRASES.get(text)
        if not tokens or sum(token in GRAMMAR_TOKENS for token in tokens) / len(tokens) < 0.7:
            matched = None
        if matched == CommandIntent.CONSENT_CONFIRM and raw_text != "check me in":
            matched = None
        if text == "stop" and len(tokens) > 2:
            matched = None
        # A partial constrained-grammar hypothesis is cheap to hallucinate from
        # office noise. Commands affect deterministic local control, so accept
        # an exact phrase only after Vosk closes the utterance at real silence.
        if not final:
            self._last_partial_intent = matched
            return None
        self._last_partial_intent = None
        return matched

    def reset(self) -> None:
        """Reset utterance state while retaining the process-wide acoustic model."""

        self._last_partial_intent = None
        if self._model is None:
            self._recognizer = None
            return
        from vosk import KaldiRecognizer

        self._recognizer = KaldiRecognizer(self._model, 16000, json.dumps(COMMAND_GRAMMAR))
