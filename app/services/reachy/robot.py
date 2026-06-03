"""Robot adapters."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.config import Settings
from app.models.domain import BoundingBox
from app.services.audio.microphone import MicrophoneAdapter
from app.services.reachy.gestures import ACK_GESTURE, DENY_GESTURE, IDLE_GESTURE
from app.services.reachy.speech import SpeechWorker
from app.services.system.telemetry import TelemetryBroker


logger = logging.getLogger(__name__)


class RobotAdapter(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def listen_chunk(self) -> bytes: ...
    async def speak(self, text: str) -> None: ...
    async def look_at_face(self, bbox: BoundingBox) -> None: ...
    async def animate_ack(self) -> None: ...
    async def animate_deny(self) -> None: ...
    async def set_idle_pose(self) -> None: ...
    def is_speaking(self) -> bool: ...


class SimulatedRobotAdapter:
    """No-hardware robot adapter for simulation and tests."""

    def __init__(self, *, settings: Settings, microphone: MicrophoneAdapter, telemetry: TelemetryBroker) -> None:
        self.settings = settings
        self.microphone = microphone
        self.telemetry = telemetry
        self.speech = SpeechWorker(speak_fn=self._perform_speak, telemetry=telemetry)

    async def start(self) -> None:
        await self.speech.start()
        self.telemetry.publish("robot", {"event": "started", "mode": "simulation"})

    async def stop(self) -> None:
        await self.speech.stop()
        self.telemetry.publish("robot", {"event": "stopped", "mode": "simulation"})

    async def listen_chunk(self) -> bytes:
        return await self.microphone.read_chunk()

    async def speak(self, text: str) -> None:
        await self.speech.submit(text)

    async def look_at_face(self, bbox: BoundingBox) -> None:
        self.telemetry.publish("robot", {"event": "look_at_face", "bbox": bbox})

    async def animate_ack(self) -> None:
        self.telemetry.publish("robot", {"event": ACK_GESTURE})

    async def animate_deny(self) -> None:
        self.telemetry.publish("robot", {"event": DENY_GESTURE})

    async def set_idle_pose(self) -> None:
        self.telemetry.publish("robot", {"event": IDLE_GESTURE})

    def is_speaking(self) -> bool:
        return self.speech.is_busy()

    async def _perform_speak(self, text: str) -> None:
        await asyncio.sleep(max(0.5, min(4.0, len(text.split()) * 0.3)))


class ReachyRobotAdapter(SimulatedRobotAdapter):
    """Reachy-backed adapter with a simulation fallback for unsupported operations."""

    def __init__(
        self,
        *,
        settings: Settings,
        microphone: MicrophoneAdapter,
        telemetry: TelemetryBroker,
    ) -> None:
        super().__init__(settings=settings, microphone=microphone, telemetry=telemetry)
        self._reachy: Any | None = None

    async def start(self) -> None:
        try:
            from reachy_mini import ReachyMini

            self._reachy = ReachyMini(media_backend="default")
            self.telemetry.publish("robot", {"event": "started", "mode": "reachy"})
        except Exception as exc:
            logger.warning("Reachy SDK unavailable, falling back to simulation mode: %s", exc)
            self._reachy = None
            self.telemetry.publish("robot", {"event": "fallback_simulation", "detail": str(exc)})
        await self.speech.start()

    async def stop(self) -> None:
        await super().stop()
        if self._reachy is not None:
            try:
                self._reachy.client.disconnect()
            except Exception:
                logger.debug("Reachy disconnect failed", exc_info=True)

    async def look_at_face(self, bbox: BoundingBox) -> None:
        await super().look_at_face(bbox)
        if self._reachy is None:
            return
        x, y, w, h = bbox
        center_x = x + (w / 2)
        center_y = y + (h / 2)
        try:
            self._reachy.look_at_image(center_x, center_y, duration=0.15)
        except Exception:
            logger.debug("Reachy look_at_image failed", exc_info=True)

    async def animate_ack(self) -> None:
        await super().animate_ack()
        if self._reachy is None:
            return
        try:
            self._reachy.goto_target(duration=0.35)
        except Exception:
            logger.debug("Reachy ack animation failed", exc_info=True)

    async def animate_deny(self) -> None:
        await super().animate_deny()

    async def set_idle_pose(self) -> None:
        await super().set_idle_pose()

    async def _perform_speak(self, text: str) -> None:
        if self.settings.tts_engine == "simulation":
            await super()._perform_speak(text)
            return

        if self.settings.tts_engine == "macos-say" and shutil.which("say"):
            process = await asyncio.create_subprocess_exec("say", text)
            await process.wait()
            return

        await super()._perform_speak(text)
