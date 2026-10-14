"""Held-out doorway audio evaluation and release gates."""

from __future__ import annotations

import json
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.config import Settings
from app.models.enums import CommandIntent
from app.services.audio.asr import CommandRecognizer
from app.services.audio.wakeword import WakeWordDetector


@dataclass(frozen=True)
class AudioSampleSpec:
    path: Path
    split: str
    expect_wake: bool = False
    expected_command: CommandIntent | None = None
    dangerous_negative: bool = False
    snr_db: float | None = None
    category: str = "unspecified"


@dataclass(frozen=True)
class AudioEvaluation:
    wake_positive_files: int
    wake_detected_files: int
    wake_recall: float | None
    negative_hours: float
    false_wakes: int
    false_wakes_per_hour: float | None
    command_positive_files_at_5db: int
    correct_command_files_at_5db: int
    command_accuracy_at_5db: float | None
    dangerous_negative_files: int
    dangerous_open_door_accepts: int
    gates: dict[str, bool]

    @property
    def passed(self) -> bool:
        return all(self.gates.values())

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "passed": self.passed}


def load_manifest(path: Path, *, split: str = "validation") -> list[AudioSampleSpec]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("samples") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Audio manifest must contain a samples array")
    specs: list[AudioSampleSpec] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("split") != split:
            continue
        relative = Path(str(row.get("path", "")))
        if not relative.as_posix() or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Audio sample paths must be relative and remain inside the corpus")
        command = row.get("expected_command")
        specs.append(
            AudioSampleSpec(
                path=(path.parent / relative).resolve(),
                split=split,
                expect_wake=bool(row.get("expect_wake", False)),
                expected_command=CommandIntent(command) if command else None,
                dangerous_negative=bool(row.get("dangerous_negative", False)),
                snr_db=float(row["snr_db"]) if row.get("snr_db") is not None else None,
                category=str(row.get("category", "unspecified")),
            )
        )
    return specs


def evaluate_corpus(settings: Settings, samples: list[AudioSampleSpec]) -> AudioEvaluation:
    wake = WakeWordDetector(
        settings.wakeword_model_path,
        engine=settings.wakeword_engine,
        vosk_model_path=settings.vosk_model_path,
        phrase=settings.wake_phrase,
        sherpa_model_dir=settings.sherpa_kws_model_dir,
        sherpa_keywords_path=settings.sherpa_kws_keywords_path,
        sherpa_threshold=settings.sherpa_kws_threshold,
        sherpa_score=settings.sherpa_kws_score,
        vosk_fallback_enabled=settings.wakeword_vosk_fallback_enabled,
    )
    command = CommandRecognizer(settings.vosk_model_path)
    wake.load()
    command.load()

    wake_positives = wake_hits = false_wakes = 0
    negative_seconds = 0.0
    command_positives = correct_commands = 0
    dangerous_files = dangerous_accepts = 0
    chunk_frames = int(settings.sample_rate_hz * settings.wakeword_chunk_ms / 1000)

    for sample in samples:
        pcm = read_pcm16_mono(
            sample.path, sample_rate=settings.sample_rate_hz, input_channel=settings.reachy_input_channel
        )
        wake.reset()
        command.reset()
        wake_events = 0
        command_events: list[CommandIntent] = []
        padded = np.concatenate((pcm, np.zeros(settings.sample_rate_hz, dtype=np.int16)))
        for offset in range(0, len(padded), chunk_frames):
            chunk = padded[offset : offset + chunk_frames].tobytes()
            if wake.score(chunk) >= settings.wakeword_threshold:
                wake_events += 1
            intent = command.recognize(chunk)
            if intent is not None:
                command_events.append(intent)

        if sample.expect_wake:
            wake_positives += 1
            wake_hits += int(wake_events > 0)
        else:
            negative_seconds += len(pcm) / settings.sample_rate_hz
            false_wakes += wake_events

        if sample.expected_command is not None and sample.snr_db is not None and sample.snr_db >= 5:
            command_positives += 1
            correct_commands += int(sample.expected_command in command_events)

        if sample.dangerous_negative:
            dangerous_files += 1
            dangerous_accepts += sum(intent == CommandIntent.OPEN_DOOR for intent in command_events)

    wake_recall = wake_hits / wake_positives if wake_positives else None
    negative_hours = negative_seconds / 3600
    false_rate = false_wakes / negative_hours if negative_hours else None
    command_accuracy = correct_commands / command_positives if command_positives else None
    gates = {
        "wake_validation_present": wake_positives > 0,
        "wake_recall_at_least_95pct": wake_recall is not None and wake_recall >= 0.95,
        "negative_audio_at_least_8h": negative_hours >= 8,
        "false_wakes_at_most_1_per_8h": false_rate is not None and false_rate <= 0.125,
        "command_5db_validation_present": command_positives > 0,
        "command_accuracy_at_least_95pct": (command_accuracy is not None and command_accuracy >= 0.95),
        "dangerous_negatives_present": dangerous_files > 0,
        "zero_dangerous_open_door_accepts": dangerous_accepts == 0,
    }
    return AudioEvaluation(
        wake_positive_files=wake_positives,
        wake_detected_files=wake_hits,
        wake_recall=wake_recall,
        negative_hours=negative_hours,
        false_wakes=false_wakes,
        false_wakes_per_hour=false_rate,
        command_positive_files_at_5db=command_positives,
        correct_command_files_at_5db=correct_commands,
        command_accuracy_at_5db=command_accuracy,
        dangerous_negative_files=dangerous_files,
        dangerous_open_door_accepts=dangerous_accepts,
        gates=gates,
    )


def read_pcm16_mono(path: Path, *, sample_rate: int, input_channel: str = "mean") -> np.ndarray:
    if input_channel not in {"left", "right", "mean"}:
        raise ValueError("Input channel must be left, right or mean")
    with wave.open(str(path), "rb") as source:
        if source.getframerate() != sample_rate or source.getsampwidth() != 2:
            raise ValueError(f"{path.name} must be PCM16 at {sample_rate} Hz")
        channels = source.getnchannels()
        if channels not in {1, 2}:
            raise ValueError(f"{path.name} must be mono or stereo")
        pcm = np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16)
    if channels == 2:
        stereo = pcm.reshape(-1, 2)
        pcm = (
            stereo[:, 0]
            if input_channel == "left"
            else stereo[:, 1]
            if input_channel == "right"
            else stereo.astype(np.int32).mean(axis=1).astype(np.int16)
        )
    return pcm
