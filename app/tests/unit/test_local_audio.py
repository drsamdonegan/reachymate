from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from app.models.enums import CommandIntent
from app.services.audio.asr import CommandRecognizer
from app.services.audio.audio_router import AudioRouter
from app.services.audio.barge_in_guard import BargeInGuard
from app.services.audio.vad import VoiceActivityDetector
from app.services.audio.wakeword import WakeWordDetector
from app.services.system.telemetry import TelemetryBroker


class FakeRecognizer:
    def __init__(self, results: list[tuple[bool, str]]) -> None:
        self.results = iter(results)
        self.current = ""

    def AcceptWaveform(self, _chunk: bytes) -> bool:  # noqa: N802
        final, self.current = next(self.results)
        return final

    def Result(self) -> str:  # noqa: N802
        return json.dumps({"text": self.current})

    def PartialResult(self) -> str:  # noqa: N802
        return json.dumps({"partial": self.current})


def test_command_recognizer_requires_a_final_exact_phrase() -> None:
    recognizer = CommandRecognizer("unused")
    recognizer._recognizer = FakeRecognizer(
        [
            (False, "[unk] open door"),
            (False, "[unk] open door"),
            (True, "[unk] open door"),
            (False, "open the door"),
            (True, "open the door"),
        ]
    )

    assert recognizer.recognize(b"audio") is None
    assert recognizer.recognize(b"audio") is None
    assert recognizer.recognize(b"audio") is None  # unknown token ratio is below 70%
    assert recognizer.recognize(b"audio") is None
    assert recognizer.recognize(b"audio") == CommandIntent.OPEN_DOOR


def test_vosk_wake_fallback_maps_reachy_to_final_richie_alias() -> None:
    detector = WakeWordDetector(engine="vosk", phrase="hey reachy")
    detector._model = object()
    detector._recognizer = FakeRecognizer([(False, "hey richie"), (True, "hey richie")])
    chunk = np.zeros(1280, dtype=np.int16).tobytes()

    assert detector.score(chunk) == 0.0
    assert detector.score(chunk) == 1.0


class OneChunkMicrophone:
    def __init__(self) -> None:
        self.read = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def read_chunk(self) -> bytes:
        if self.read:
            await asyncio.Event().wait()
        self.read = True
        return b"wake"


class FixedScore:
    def __init__(self, value: float) -> None:
        self.value = value

    def score(self, _chunk: bytes) -> float:
        return self.value


class NoCommand:
    def recognize(self, _chunk: bytes):  # type: ignore[no-untyped-def]
        return None


class CountingCommand:
    def __init__(self) -> None:
        self.calls = 0

    def recognize(self, _chunk: bytes):  # type: ignore[no-untyped-def]
        self.calls += 1
        return CommandIntent.OPEN_DOOR


@pytest.mark.asyncio
@pytest.mark.parametrize("attended", [True, False])
async def test_attended_audio_has_no_camera_gaps_but_local_commands_stay_gated(settings, attended):
    guard = BargeInGuard(0)
    guard.begin_engagement(10)
    recognizer = CountingCommand()
    forwarded = []
    got_audio = asyncio.Event()

    def receive(chunk):
        forwarded.append(chunk)
        got_audio.set()
        return True

    router = AudioRouter(
        settings=settings,
        microphone=OneChunkMicrophone(),
        vad=FixedScore(1),
        wakeword=FixedScore(0),
        asr=recognizer,
        guard=guard,
        telemetry=TelemetryBroker(),
        robot_is_speaking=lambda: False,
        face_present_supplier=lambda: False,
        on_wake=lambda: asyncio.sleep(0),
        on_command=lambda _: asyncio.sleep(0),
        on_engaged_audio=receive,
        attended_chat_supplier=lambda: attended,
    )
    await router.start()
    try:
        await asyncio.wait_for(got_audio.wait(), 2)
        assert forwarded == [b"wake" if attended else bytes(4)]
        assert recognizer.calls == 0
        assert router.diagnostics()["gate_reason"] == ("attended_chat" if attended else "no_engaged_face")
    finally:
        await router.stop()


class ContinuousSpeechVadModel:
    def __init__(self) -> None:
        self.reset_calls = 0

    def accept_waveform(self, _samples) -> None:  # type: ignore[no-untyped-def]
        return None

    def is_speech_detected(self) -> bool:
        return True

    def empty(self) -> bool:
        return True

    def pop(self) -> None:
        return None

    def reset(self) -> None:
        self.reset_calls += 1


def test_vad_resets_continuous_noise_before_internal_buffer_can_grow() -> None:
    detector = VoiceActivityDetector("unused")
    model = ContinuousSpeechVadModel()
    detector._model = model
    chunk = np.zeros(512, dtype=np.int16).tobytes()

    for _ in range(125):
        detector.score(chunk)

    assert model.reset_calls == 1


@pytest.mark.asyncio
async def test_wake_does_not_require_a_face(settings) -> None:
    woke = asyncio.Event()

    async def on_wake() -> None:
        woke.set()

    async def on_command(_command: CommandIntent) -> None:
        return None

    router = AudioRouter(
        settings=settings,
        microphone=OneChunkMicrophone(),  # type: ignore[arg-type]
        vad=FixedScore(1.0),  # type: ignore[arg-type]
        wakeword=FixedScore(1.0),  # type: ignore[arg-type]
        asr=NoCommand(),  # type: ignore[arg-type]
        guard=BargeInGuard(0),
        telemetry=TelemetryBroker(),
        robot_is_speaking=lambda: False,
        face_present_supplier=lambda: False,
        on_wake=on_wake,
        on_command=on_command,
    )
    await router.start()
    try:
        await asyncio.wait_for(woke.wait(), timeout=1)
    finally:
        await router.stop()


@pytest.mark.asyncio
async def test_local_commands_ignore_robot_playback_while_realtime_audio_continues(settings) -> None:
    guard = BargeInGuard(0)
    guard.begin_engagement(10)
    recognizer = CountingCommand()
    forwarded: list[bytes] = []
    command_seen = asyncio.Event()

    async def on_command(_command: CommandIntent) -> None:
        command_seen.set()

    import time

    from app.services.vision.attention import AttentionObservation
    from app.services.vision.av_gate import AVGate

    router = AudioRouter(
        settings=settings,
        microphone=OneChunkMicrophone(),  # type: ignore[arg-type]
        vad=FixedScore(1.0),  # type: ignore[arg-type]
        wakeword=FixedScore(0.0),  # type: ignore[arg-type]
        asr=recognizer,  # type: ignore[arg-type]
        guard=guard,
        telemetry=TelemetryBroker(),
        robot_is_speaking=lambda: True,
        face_present_supplier=lambda: False,
        on_wake=lambda: asyncio.sleep(0),
        on_command=on_command,
        on_engaged_audio=lambda chunk: forwarded.append(chunk) is None,
        av_gate=AVGate(attack_seconds=0),
        attention_supplier=lambda: AttentionObservation(1, (0, 0, 150, 150), 0.0, time.monotonic()),
    )
    await router.start()
    try:
        await asyncio.sleep(0.05)
    finally:
        await router.stop()

    assert forwarded == [b"wake"]
    assert recognizer.calls == 0
    assert not command_seen.is_set()
