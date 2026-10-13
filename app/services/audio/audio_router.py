"""Supervised local inference and fail-closed visitor audio routing."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.config import Settings
from app.models.enums import CommandIntent
from app.services.audio.asr import CommandRecognizer
from app.services.audio.barge_in_guard import BargeInGuard
from app.services.audio.microphone import MicrophoneAdapter
from app.services.audio.vad import VoiceActivityDetector
from app.services.audio.wakeword import WakeWordDetector
from app.services.reachy.media import AudioStalled
from app.services.system.heartbeat import Heartbeat
from app.services.system.supervisor import SupervisedTask
from app.services.system.telemetry import TelemetryBroker
from app.services.vision.av_gate import AVGate

logger = logging.getLogger(__name__)


class AudioRouter:
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
        on_engaged_audio: Callable[[bytes], bool] | None = None,
        enabled: bool = True,
        av_gate: AVGate | None = None,
        attention_supplier=None,
        doa_supplier=None,
        on_activity=None,
        on_fault=None,
        on_track_changed=None,
        on_barge_in=None,
        barge_in_allowed=None,
    ) -> None:
        self.settings, self.microphone, self.vad, self.wakeword, self.asr = (
            settings,
            microphone,
            vad,
            wakeword,
            asr,
        )
        self.guard, self.telemetry, self.robot_is_speaking = guard, telemetry, robot_is_speaking
        self.face_present_supplier = face_present_supplier
        self.on_wake, self.on_command, self.on_engaged_audio = on_wake, on_command, on_engaged_audio
        self.enabled = enabled
        self.av_gate = av_gate or AVGate(sample_rate=settings.sample_rate_hz)
        self.attention_supplier, self.doa_supplier = attention_supplier, doa_supplier
        self.on_activity, self.on_fault, self.on_track_changed = on_activity, on_fault, on_track_changed
        self.on_barge_in, self.barge_in_allowed = on_barge_in, barge_in_allowed
        self._barge_speech_seconds = 0.0
        self._barge_latched = False
        self._task = None
        self._callback_tasks: set[asyncio.Task] = set()
        self._running = False
        self._executor: ThreadPoolExecutor | None = None
        self.heartbeat = Heartbeat(0.08)
        self._supervisor = SupervisedTask(
            "audio-router", self._run, heartbeat=self.heartbeat, on_fault=self._recover
        )
        self._last_diag: dict[str, Any] = {"vad_score": 0.0, "wake_score": 0.0, "last_command": None}
        self._speech_window: deque[tuple[float, float, bool]] = deque()
        self._wakes: deque[float] = deque()
        self._last_wake = -float("inf")
        self._storm_until = 0.0
        self._was_engaged = False
        self._last_track = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-audio-inference")
        await self.microphone.start()
        if self.enabled:
            await self._supervisor.start()
            self._task = self._supervisor.task

    def set_enabled(self, enabled: bool) -> None:
        if self._task is not None:
            raise RuntimeError("Cannot change audio routing while running")
        self.enabled = enabled

    async def stop(self) -> None:
        self._running = False
        await self._supervisor.stop()
        self._task = None
        for task in tuple(self._callback_tasks):
            task.cancel()
        await asyncio.gather(*self._callback_tasks, return_exceptions=True)
        self._callback_tasks.clear()
        await self.microphone.stop()
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self.av_gate.reset()

    def diagnostics(self) -> dict[str, Any]:
        return {
            **self._last_diag,
            "running": self._running
            and (not self.enabled or self._task is not None and not self._task.done()),
            "heartbeat": self.heartbeat.diagnostics(),
            "restarts": len(self._supervisor.restarts),
            "restart_limit": self._supervisor.error,
        }

    async def _recover(self, reason: str) -> None:
        self.telemetry.publish("audio", {"event": "audio_failed", "reason": reason})
        media = getattr(self.microphone, "media", self.microphone)
        restart = getattr(media, "restart_streams", None)
        try:
            if restart:
                await restart()
            else:
                await self.microphone.stop()
                await self.microphone.start()
        except Exception as exc:
            self.heartbeat.fail(type(exc).__name__)
            detail = str(exc).casefold()
            fatal = (
                "tcc_denied" in detail
                or "reboot_limit" in detail
                or "usb" in detail
                and ("unavailable" in detail or "not found" in detail)
            )
            if self.on_fault and fatal:
                await self.on_fault("audio_recovery_requires_operator")
        if reason == "restart_limit" and self.on_fault:
            await self.on_fault(reason)

    async def _infer(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self._executor, function, *args)

    async def _run(self) -> None:
        while self._running:
            try:
                chunk = await asyncio.wait_for(self.microphone.read_chunk(), timeout=2.1)
            except TimeoutError as exc:
                raise AudioStalled("microphone_read_timeout") from exc
            now = time.monotonic()
            engaged = self.guard.is_engaged()
            if not engaged and self._was_engaged:
                self.av_gate.reset()
                self._last_track = None
            self._was_engaged = engaged
            vad_score = await self._infer(self.vad.score, chunk)
            duration = len(chunk) / (2 * self.settings.sample_rate_hz)
            # Some test microphones use symbolic chunks; real chunks are PCM16.
            duration = max(duration, self.settings.wakeword_chunk_ms / 1000)
            self._speech_window.append((now, duration, vad_score >= self.settings.vad_threshold))
            while self._speech_window and self._speech_window[0][0] < now - 1:
                self._speech_window.popleft()
            if self._storm_until and now > self._storm_until:
                setter = getattr(self.wakeword, "set_threshold_offset", None)
                if setter:
                    await self._infer(setter, 0.0)
                self._storm_until = 0.0
                self._wakes.clear()
            wake_score = await self._infer(self.wakeword.score, chunk) if not engaged else 0.0
            self.heartbeat.beat()
            self._last_diag = {
                "vad_score": round(vad_score, 4),
                "wake_score": round(wake_score, 4),
                "engaged": engaged,
            }
            if engaged:
                face = self.attention_supplier() if self.attention_supplier else None
                doa = self.doa_supplier() if self.doa_supplier else None
                result = self.av_gate.process(chunk, vad_score=vad_score, face=face, doa=doa)
                self._last_diag.update(
                    {"gate_open": result.opened, "gate_reason": result.reason, "r2_degraded": result.degraded}
                )
                if self.on_track_changed:
                    fresh_track = (
                        str(face.track_id) if face is not None and now - face.timestamp < 2 else None
                    )
                    update = self.on_track_changed(fresh_track)
                    if hasattr(update, "__await__"):
                        await update
                self._last_track = face.track_id if face is not None else None
                if self.on_engaged_audio:
                    for gated in result.chunks:
                        self.on_engaged_audio(gated)
                if result.opened and vad_score >= self.settings.vad_threshold:
                    self._wakes.clear()  # Storms count only wakes with no visitor turn.
                    if self.on_activity:
                        activity = self.on_activity()
                        if hasattr(activity, "__await__"):
                            await activity
                speaking = self.robot_is_speaking()
                allowed = self.barge_in_allowed is None or self.barge_in_allowed()
                if not speaking:
                    self._barge_latched = False
                    self._barge_speech_seconds = 0.0
                elif result.opened and vad_score >= 0.6 and allowed:
                    self._barge_speech_seconds += duration
                    if self._barge_speech_seconds >= 0.16 and not self._barge_latched and self.on_barge_in:
                        self._barge_latched = True
                        self._dispatch(self.on_barge_in(), name="gated-local-barge-in")
                else:
                    self._barge_speech_seconds = 0.0
                # Local access/cancel grammar receives exactly the same gated
                # visitor speech. Playback cannot affirm biometric consent.
                if result.opened and not self.robot_is_speaking():
                    command = None
                    for gated in result.chunks:
                        detected = await self._infer(self.asr.recognize, gated)
                        command = detected or command
                    if command is not None:
                        self._last_diag["last_command"] = command.value
                        self.telemetry.publish("heard", {"text": command.value, "source": "command"})
                        self._dispatch(self.on_command(command), name=f"command-{command.name.lower()}")
                continue
            if not self.guard.can_trigger(robot_speaking=self.robot_is_speaking()):
                continue
            speech_duration = sum(d for _, d, yes in self._speech_window if yes)
            total_duration = sum(d for _, d, _ in self._speech_window)
            if (
                wake_score < self.settings.wakeword_threshold
                or speech_duration / max(total_duration, 1e-6) < 0.3
                or now - self._last_wake < 2
            ):
                continue
            self._last_wake = now
            self._wakes.append(now)
            while self._wakes and self._wakes[0] < now - 600:
                self._wakes.popleft()
            if len(self._wakes) > 6 and now > self._storm_until:
                self._storm_until = now + 1800
                setter = getattr(self.wakeword, "set_threshold_offset", None)
                if setter:
                    await self._infer(setter, 0.05)
                self.telemetry.publish("audio", {"event": "wake_storm", "threshold_offset": 0.05})
            self.guard.begin_engagement(self.settings.engagement_timeout_seconds)
            self.telemetry.publish("heard", {"text": "Hey Reachy", "source": "wakeword"})
            self._dispatch(self.on_wake(), name="wake-engagement")

    def _dispatch(self, awaitable: Awaitable[None], *, name: str) -> None:
        task = asyncio.create_task(awaitable, name=name)
        self._callback_tasks.add(task)

        def finished(done):
            self._callback_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.warning("%s failed: %s", name, type(done.exception()).__name__)

        task.add_done_callback(finished)
