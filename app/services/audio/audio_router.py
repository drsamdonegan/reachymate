"""Always-on audio router."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Settings
from app.models.enums import CommandIntent
from app.services.audio.asr import CommandRecognizer
from app.services.audio.barge_in_guard import BargeInGuard
from app.services.audio.microphone import MicrophoneAdapter
from app.services.audio.vad import VoiceActivityDetector
from app.services.audio.wakeword import WakeWordDetector
from app.services.system.telemetry import TelemetryBroker


logger = logging.getLogger(__name__)


class AudioRouter:
    """Route audio through VAD, wakeword, and command ASR."""

    def __init__(
        self,
        *,
        settings: Settings,
        microphone: MicrophoneAdapter,
        vad: VoiceActivityDetector,
        wakeword: WakeWordDetector,
        asr: CommandRecognizer,
        guard: BargeInGuard,
        telemetry: TelemetryBroker,
        robot_is_speaking: Callable[[], bool],
        face_present_supplier: Callable[[], bool],
        on_wake: Callable[[], Awaitable[None]],
        on_command: Callable[[CommandIntent], Awaitable[None]],
    ) -> None:
        self.settings = settings
        self.microphone = microphone
        self.vad = vad
        self.wakeword = wakeword
        self.asr = asr
        self.guard = guard
        self.telemetry = telemetry
        self.robot_is_speaking = robot_is_speaking
        self.face_present_supplier = face_present_supplier
        self.on_wake = on_wake
        self.on_command = on_command
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last_diag: dict[str, Any] = {"vad_score": 0.0, "wake_score": 0.0, "last_command": None}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        await self.microphone.start()
        self._task = asyncio.create_task(self._run(), name="audio-router")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.microphone.stop()

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._last_diag)

    async def _run(self) -> None:
        while self._running:
            chunk = await self.microphone.read_chunk()
            vad_score = self.vad.score(chunk)
            wake_score = 0.0
            if vad_score >= self.settings.vad_threshold / 2:
                wake_score = self.wakeword.score(chunk)

            self._last_diag = {
                "vad_score": round(vad_score, 4),
                "wake_score": round(wake_score, 4),
                "engaged": self.guard.is_engaged(),
            }

            if not self.guard.can_trigger(robot_speaking=self.robot_is_speaking()):
                continue

            if wake_score >= self.settings.wakeword_threshold and self.face_present_supplier():
                self.guard.begin_engagement(self.settings.engagement_timeout_seconds)
                self.telemetry.publish("heard", {"text": "Hey Reachy", "source": "wakeword"})
                await self.on_wake()
                continue

            if self.guard.is_engaged():
                command = self.asr.recognize(chunk)
                if command is not None:
                    self._last_diag["last_command"] = command.value
                    self.telemetry.publish("heard", {"text": command.value, "source": "command"})
                    await self.on_command(command)
