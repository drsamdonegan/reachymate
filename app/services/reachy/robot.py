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
    async def wake(self, bearing_radians: float = 0.0) -> None: ...
    async def park(self) -> None: ...
    def is_speaking(self) -> bool: ...


class SimulatedRobotAdapter:
    """No-hardware robot adapter for simulation and tests."""

    def __init__(
        self, *, settings: Settings, microphone: MicrophoneAdapter, telemetry: TelemetryBroker
    ) -> None:
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

    async def wake(self, bearing_radians: float = 0.0) -> None:
        self.telemetry.publish("robot", {"event": "wake", "bearing": bearing_radians})

    async def park(self) -> None:
        self.telemetry.publish("robot", {"event": "park", "torque": "disabled"})

    def set_capture_freeze(self, enabled: bool) -> None:
        self.telemetry.publish("robot", {"event": "capture_freeze", "enabled": enabled})

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
        media=None,
    ) -> None:
        super().__init__(settings=settings, microphone=microphone, telemetry=telemetry)
        self.media = media
        self._reachy: Any | None = None
        self.motion = None
        self.power = None
        self.daemon = None
        self.wobbler = None
        self.calibration = None
        self._realtime_speech: Callable[[str], Awaitable[None]] | None = None

    def set_realtime_speech(self, speak: Callable[[str], Awaitable[None]]) -> None:
        self._realtime_speech = speak

    async def start(self) -> None:
        if self.media is None:
            raise RuntimeError("Reachy robot requires the shared media connection")
        await self.media.connect()
        self._reachy = self.media.reachy
        from app.services.reachy.daemon_client import DaemonClient
        from app.services.reachy.motion.head_wobbler import HeadWobbler
        from app.services.reachy.motion.manager import MotionManager
        from app.services.reachy.power import RobotPowerManager
        from app.services.vision.attention import CameraCalibration

        self.daemon = DaemonClient(self.settings.reachy_host, self.settings.reachy_port)
        self.motion = MotionManager(self._reachy)
        self.motion.start()
        self.power = RobotPowerManager(self.motion, self.daemon)
        path = getattr(self.settings, "camera_calibration_path", None)
        if path:
            self.calibration = CameraCalibration.load(path)
        self.wobbler = HeadWobbler(self.motion.set_speech_offsets, sample_rate=self.settings.sample_rate_hz)
        self.wobbler.start()
        self.media.set_output_tap(self.wobbler.feed_samples, self._reset_speech)
        # Startup repair parks a robot left awake after a previous client crash.
        await self.power.park()
        self.telemetry.publish("robot", {"event": "started", "mode": "reachy"})
        await self.speech.start()

    async def stop(self) -> None:
        try:
            await self.park()
        finally:
            await super().stop()
            if self.wobbler:
                await asyncio.to_thread(self.wobbler.stop)
            if self.motion:
                await self.motion.stop()
            if self.media is not None:
                await self.media.disconnect()
            if self.daemon:
                self.daemon.close()
            self._reachy = None

    async def look_at_face(self, bbox: BoundingBox) -> None:
        if self.motion is None or self.calibration is None:
            return
        pose = await self.motion.execute(self._face_target, bbox)
        self.motion.set_tracking_target(pose)

    def _face_target(self, bbox):
        import numpy as np

        camera_direction = self.calibration.direction(bbox)
        # Camera bearings are relative to the moving head. Transform back into
        # the robot frame before asking the SDK for a world look target.
        head_pose = self._reachy.get_current_head_pose()
        world = head_pose @ np.array([*camera_direction, 1.0])
        return self._reachy.look_at_world(
            x=float(world[0]), y=float(world[1]), z=float(world[2]), perform_movement=False
        )

    async def animate_ack(self) -> None:
        await super().animate_ack()
        await self._gesture(ACK_GESTURE)

    async def animate_deny(self) -> None:
        await super().animate_deny()
        await self._gesture(DENY_GESTURE)

    async def _gesture(self, name: str) -> None:
        if self.motion is None:
            return
        from app.services.reachy.gestures import gesture_poses
        from app.services.reachy.motion.manager import MotionMode

        if self.motion.mode in {MotionMode.PARKED, MotionMode.CAPTURE_FREEZE}:
            return
        for pose in gesture_poses(name):
            await self.motion.execute(self._reachy.goto_target, head=pose, duration=0.25, body_yaw=None)

    async def wake(self, bearing_radians: float = 0.0) -> None:
        if self.power:
            await self.power.wake(bearing_radians)

    def _reset_speech(self) -> None:
        if self.wobbler:
            self.wobbler.reset()
        if self.motion:
            self.motion.reset_speech()

    async def park(self) -> None:
        if self.power:
            self._reset_speech()
            await self.power.park()

    def set_capture_freeze(self, enabled: bool) -> None:
        from app.services.reachy.motion.manager import MotionMode

        if self.motion:
            self.motion.set_mode(MotionMode.CAPTURE_FREEZE if enabled else MotionMode.ENGAGED)

    async def set_idle_pose(self) -> None:
        await super().set_idle_pose()
        from app.services.reachy.motion.manager import MotionMode

        if self.motion:
            self.motion.set_mode(MotionMode.IDLE_NEUTRAL)

    def is_speaking(self) -> bool:
        realtime_output_active = bool(
            self.media is not None and getattr(self.media, "output_active", lambda: False)()
        )
        return super().is_speaking() or realtime_output_active

    async def _perform_speak(self, text: str) -> None:
        if self.settings.tts_engine == "simulation":
            await super()._perform_speak(text)
            return

        if self.settings.tts_engine == "macos-say" and shutil.which("say"):
            # SDK native media frameworks are already loaded: use an absolute
            # executable and posix_spawn rather than fork on macOS.
            process = await asyncio.create_subprocess_exec(shutil.which("say"), text, close_fds=False)
            await process.wait()
            return

        if self.settings.tts_engine == "realtime":
            if self._realtime_speech is None:
                raise RuntimeError("Realtime speech is not connected to the robot adapter")
            owner = getattr(self._realtime_speech, "__self__", None)
            if owner is not None and not getattr(owner, "running", False):
                self.telemetry.publish("speech", {"event": "speech_skipped", "reason": "no_realtime_session"})
                return
            await self._realtime_speech(text)
            return

        await super()._perform_speak(text)
