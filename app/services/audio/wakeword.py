"""Wakeword detection."""

from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np


class WakeWordDetector:
    """Local openWakeWord or grammar-constrained Vosk wake detector."""

    def __init__(
        self,
        model_path: str | None = None,
        *,
        engine: str = "openwakeword",
        vosk_model_path: str | None = None,
        phrase: str = "hey reachy",
        sherpa_model_dir: str | None = None,
        sherpa_keywords_path: str | None = None,
        sherpa_threshold: float = 0.25,
        sherpa_score: float = 1.5,
        vosk_fallback_enabled: bool = False,
    ) -> None:
        self.model_path = model_path
        self.engine = engine.strip().casefold()
        self.vosk_model_path = vosk_model_path
        self.phrase = " ".join(phrase.casefold().split())
        self.sherpa_model_dir = sherpa_model_dir
        self.sherpa_keywords_path = sherpa_keywords_path
        self.sherpa_threshold = sherpa_threshold
        self.sherpa_score = sherpa_score
        self.vosk_fallback_enabled = vosk_fallback_enabled
        self._model: Any | None = None
        self._recognizer: Any | None = None
        self._fallback_recognizer: Any | None = None
        self._load_error: str | None = None
        self._stream: Any | None = None
        self._threshold_offset = 0.0
        self._audio_ring: deque[bytes] = deque(maxlen=25)
        self._confirmation_remaining = 0
        self._confirmation_audio: list[bytes] = []

    @property
    def available(self) -> bool:
        return self._model is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def load(self) -> None:
        if self.available:
            return
        try:
            if self.engine == "openwakeword":
                if not self.model_path or not Path(self.model_path).is_file():
                    raise FileNotFoundError("custom openWakeWord model is missing")
                from openwakeword.model import Model

                self._model = Model(wakeword_models=[self.model_path])
            elif self.engine == "vosk":
                if not self.vosk_model_path or not Path(self.vosk_model_path).is_dir():
                    raise FileNotFoundError("Vosk wake model is missing")
                from app.services.audio.vosk_model import load_vosk_model

                self._model = load_vosk_model(self.vosk_model_path)
                self._recognizer = self._new_vosk_recognizer(self._model)
            elif self.engine == "sherpa_onnx":
                self._load_sherpa()
            else:
                raise ValueError(f"unsupported wakeword engine: {self.engine}")
        except Exception as exc:
            self._model = None
            self._recognizer = None
            self._fallback_recognizer = None
            self._load_error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(f"Wake word detector failed to load: {self._load_error}") from exc
        self._load_error = None

    def score(self, chunk: bytes) -> float:
        pcm_int16 = np.frombuffer(chunk, dtype=np.int16)
        pcm = pcm_int16.astype(np.float32) / 32768.0
        if pcm.size == 0:
            return 0.0

        if not self.available:
            try:
                self.load()
            except Exception:
                return 0.0

        if self.engine == "vosk":
            return self._score_vosk(pcm_int16.tobytes(), self._recognizer)
        if self.engine == "sherpa_onnx":
            current = pcm_int16.tobytes()
            self._audio_ring.append(current)
            sherpa_score = self._score_sherpa(pcm)
            if not self.vosk_fallback_enabled:
                return sherpa_score
            # Optional confirmer sees -1.6s to +0.4s around the KWS hit.
            # It is never an alternative wake source.
            if self._confirmation_remaining:
                self._confirmation_audio.append(current)
                self._confirmation_remaining -= len(pcm_int16)
                if self._confirmation_remaining <= 0:
                    recognizer = self._fallback_recognizer
                    recognizer.AcceptWaveform(b"".join(self._confirmation_audio))
                    text = " ".join(json.loads(recognizer.FinalResult()).get("text", "").split()).casefold()
                    self._confirmation_audio.clear()
                    recognizer.Reset()
                    return 1.0 if text in self._vosk_phrases() else 0.0
                return 0.0
            if sherpa_score:
                raw = b"".join(self._audio_ring)
                self._confirmation_audio = [raw[-int(16000 * 1.6) * 2 :]]
                self._confirmation_remaining = int(16000 * 0.4)
            return 0.0

        try:
            predictions = self._model.predict(pcm)
            if not predictions:
                return 0.0
            return max(float(score) for score in predictions.values())
        except Exception:
            return 0.0

    def _score_vosk(self, chunk: bytes, recognizer: Any | None) -> float:
        if recognizer is None:
            return 0.0
        try:
            if not recognizer.AcceptWaveform(chunk):
                return 0.0
            result = json.loads(recognizer.Result())
            text = " ".join((result.get("text") or "").split()).casefold()
        except Exception:
            return 0.0
        if text in self._vosk_phrases():
            return 1.0
        return 0.0

    def _load_sherpa(self) -> None:
        if not self.sherpa_model_dir or not Path(self.sherpa_model_dir).is_dir():
            raise FileNotFoundError("sherpa-onnx keyword model is missing")
        if not self.sherpa_keywords_path or not Path(self.sherpa_keywords_path).is_file():
            raise FileNotFoundError("sherpa-onnx keyword definition is missing")
        import sherpa_onnx

        root = Path(self.sherpa_model_dir)
        self._model = sherpa_onnx.KeywordSpotter(
            tokens=str(root / "tokens.txt"),
            encoder=str(root / "encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx"),
            decoder=str(root / "decoder-epoch-13-avg-2-chunk-8-left-64.onnx"),
            joiner=str(root / "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx"),
            keywords_file=self.sherpa_keywords_path,
            num_threads=2,
            sample_rate=16000,
            max_active_paths=4,
            keywords_score=self.sherpa_score,
            keywords_threshold=self.sherpa_threshold,
            num_trailing_blanks=1,
            provider="cpu",
        )
        self._stream = self._model.create_stream()
        if self.vosk_fallback_enabled:
            if not self.vosk_model_path or not Path(self.vosk_model_path).is_dir():
                raise FileNotFoundError("Vosk wake fallback model is missing")
            from app.services.audio.vosk_model import load_vosk_model

            vosk_model = load_vosk_model(self.vosk_model_path)
            self._fallback_recognizer = self._new_vosk_recognizer(vosk_model)

    def _score_sherpa(self, pcm: np.ndarray) -> float:
        if self._stream is None:
            return 0.0
        try:
            self._stream.accept_waveform(16000, pcm)
            while self._model.is_ready(self._stream):
                self._model.decode_stream(self._stream)
                result = self._model.get_result(self._stream)
                if result:
                    self._model.reset_stream(self._stream)
                    return 1.0
        except Exception:
            return 0.0
        return 0.0

    def set_threshold_offset(self, offset: float) -> None:
        self._threshold_offset = max(0.0, min(0.2, offset))
        if self.engine == "sherpa_onnx" and self._model is not None:
            keywords = Path(self.sherpa_keywords_path).read_text()
            keywords = re.sub(
                r"#([0-9.]+)",
                lambda match: "#" + str(min(0.9, float(match.group(1)) + self._threshold_offset)),
                keywords,
            )
            self._stream = self._model.create_stream(keywords=keywords)

    def reset(self) -> None:
        """Reset stream state between independently labelled recordings."""

        self._audio_ring.clear()
        self._confirmation_remaining = 0
        self._confirmation_audio.clear()
        if self.engine == "sherpa_onnx" and self._model is not None:
            self.set_threshold_offset(self._threshold_offset)
            if self._fallback_recognizer is not None:
                from app.services.audio.vosk_model import load_vosk_model

                self._fallback_recognizer = self._new_vosk_recognizer(
                    load_vosk_model(str(self.vosk_model_path))
                )
        elif self.engine == "vosk" and self._model is not None:
            self._recognizer = self._new_vosk_recognizer(self._model)

    def _vosk_phrases(self) -> tuple[str, ...]:
        """Map the product name to an in-vocabulary acoustic spelling."""

        aliases = [self.phrase, "hi richie", "hello richie"]
        if "reachy" in self.phrase:
            aliases.append(self.phrase.replace("reachy", "richie"))
        return tuple(dict.fromkeys(aliases))

    def _new_vosk_recognizer(self, model: Any) -> Any:
        from vosk import KaldiRecognizer

        # The small English model does not contain the proper-name spelling
        # "reachy". Only place in-vocabulary aliases in its decoding graph.
        grammar_phrases = [phrase for phrase in self._vosk_phrases() if "reachy" not in phrase]
        if not grammar_phrases:
            grammar_phrases = [self.phrase]
        return KaldiRecognizer(model, 16000, json.dumps([*grammar_phrases, "[unk]"]))
