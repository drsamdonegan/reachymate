"""One thread owns SDK motion, including all blocking SDK goto calls."""

from __future__ import annotations

import asyncio
import math
import queue
import threading
import time
from concurrent.futures import Future
from enum import Enum

import numpy as np

from app.services.reachy.motion.expressions import Frame, MotionSequence, reachable_joints, safe_frame


class MotionMode(str, Enum):
    PARKED = "parked"
    IDLE_NEUTRAL = "idle_neutral"
    ENGAGED = "engaged"
    CAPTURE_FREEZE = "capture_freeze"


def clamp_pose(pose):
    from reachy_mini.utils.rotation import Rotation

    value = np.asarray(pose, dtype=float).copy()
    if value.shape != (4, 4) or not np.isfinite(value).all():
        raise ValueError("Invalid head pose")
    roll, pitch, yaw = Rotation.from_matrix(value[:3, :3]).as_euler("xyz")
    rpy = np.clip([roll, pitch, yaw], np.radians([-10, -15, -35]), np.radians([10, 20, 35]))
    value[:3, :3] = Rotation.from_euler("xyz", rpy).as_matrix()
    value[:3, 3] = np.clip(value[:3, 3], [-0.015, -0.015, -0.015], [0.015, 0.015, 0.015])
    return value


class MotionManager:
    def __init__(self, mini, *, on_fault=None) -> None:
        self.mini, self.on_fault = mini, on_fault
        self.mode = MotionMode.PARKED
        self.target = np.eye(4)
        self._offsets = (0.0,) * 6
        self._offsets_at = 0.0
        self._idle_motion_enabled = False
        self._lock = threading.Lock()
        self._commands: queue.Queue = queue.Queue(maxsize=32)
        self._stop = threading.Event()
        self.thread = None
        self.in_flight = False
        self.last_motion_at = 0.0
        self.last_success = None
        self.error = None
        self._expression: MotionSequence | None = None
        self._expression_started = 0.0
        self._held_frame: Frame | None = None
        self._frame = Frame.neutral()
        self.expression_status = "idle"
        self.expression_label: str | None = None
        self.expression_amplitude_scale = 1.0
        self._expression_generation = 0

    def start(self) -> None:
        if self.thread is None:
            self._stop.clear()
            self.thread = threading.Thread(target=self._run, name="motion-arbiter", daemon=True)
            self.thread.start()

    async def stop(self) -> None:
        self.stop_expression()
        self._stop.set()
        if self.thread:
            await asyncio.to_thread(self.thread.join, 3)
            if self.thread.is_alive():
                raise RuntimeError("Motion arbiter did not stop")
            self.thread = None

    def enqueue(self, function, *args, **kwargs) -> Future:
        future = Future()
        self._commands.put_nowait((future, function, args, kwargs))
        return future

    async def execute(self, function, *args, **kwargs):
        return await asyncio.wrap_future(self.enqueue(function, *args, **kwargs))

    def set_mode(self, mode: MotionMode) -> None:
        with self._lock:
            self.mode = mode
            self._idle_motion_enabled = mode == MotionMode.ENGAGED
            if mode in {MotionMode.PARKED, MotionMode.CAPTURE_FREEZE}:
                self._offsets = (0.0,) * 6
            if mode != MotionMode.ENGAGED:
                self._expression_generation += 1
                self._expression = self._held_frame = None
                self.expression_status = "cancelled"

    def frame(self) -> Frame:
        with self._lock:
            return self._frame.copy()

    def rebase_frame(self, head, *, body=0.0) -> None:
        with self._lock:
            self._frame = Frame(np.asarray(head).copy(), np.radians([-10.0, 10.0]), body)

    def reserve_expression(self) -> int | None:
        with self._lock:
            if self.mode != MotionMode.ENGAGED or self.in_flight:
                return None
            self._expression_generation += 1
            self._idle_motion_enabled = True
            self._expression = None
            self._held_frame = self._frame.copy()
            return self._expression_generation

    def play_expression(self, sequence: MotionSequence, *, generation: int | None = None) -> bool:
        with self._lock:
            if self.mode != MotionMode.ENGAGED or self.in_flight:
                return False
            if generation is not None and generation != self._expression_generation:
                return False
            self._expression, self._expression_started = sequence, time.monotonic()
            self._held_frame = None
            self.expression_label, self.expression_status = sequence.label, "running"
            self.expression_amplitude_scale = sequence.amplitude_scale
            return True

    def stop_expression(self) -> None:
        with self._lock:
            self._idle_motion_enabled = False  # Stop means stop speech/breathing motion too.
            self._expression_generation += 1
            self._expression = None
            self._held_frame = self._frame.copy() if self.mode == MotionMode.ENGAGED else None
            self.expression_status = "stopped"

    def expression_diagnostics(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode.value,
                "status": self.expression_status,
                "name": self.expression_label,
                "amplitude_scale": self.expression_amplitude_scale,
                "active": self._expression is not None,
                "speech_motion_enabled": self.mode == MotionMode.ENGAGED and self._idle_motion_enabled,
                "speech_active": self.mode == MotionMode.ENGAGED
                and self._idle_motion_enabled
                and self._expression is None
                and time.monotonic() - self._offsets_at < 0.3
                and any(abs(x) > 0.0001 for x in self._offsets),
                "error": self.error,
                "remaining_seconds": max(
                    0, self._expression.duration - (time.monotonic() - self._expression_started)
                )
                if self._expression
                else 0,
            }

    def set_tracking_target(self, pose) -> None:
        with self._lock:
            if self.mode != MotionMode.CAPTURE_FREEZE:
                self.target = clamp_pose(pose)

    def set_speech_offsets(self, offsets) -> None:
        with self._lock:
            if self.mode == MotionMode.ENGAGED:
                self._offsets = tuple(offsets)
                self._offsets_at = time.monotonic()

    def reset_speech(self) -> None:
        with self._lock:
            self._offsets = (0.0,) * 6

    def _run(self) -> None:
        from reachy_mini.kinematics.analytical_kinematics import AnalyticalKinematics
        from reachy_mini.utils import create_head_pose

        kinematics = AnalyticalKinematics(automatic_body_yaw=False)

        next_frame = time.monotonic()
        while not self._stop.is_set():
            try:
                future, function, args, kwargs = self._commands.get_nowait()
            except queue.Empty:
                future = None
            if future is not None:
                if not future.set_running_or_notify_cancel():
                    continue
                self.in_flight = True
                try:
                    future.set_result(function(*args, **kwargs))
                    self.last_success = time.monotonic()
                except Exception as exc:
                    self.error = type(exc).__name__
                    future.set_exception(exc)
                finally:
                    self.in_flight = False
                    if getattr(function, "__name__", "") in {
                        "goto_target",
                        "goto_sleep",
                        "wake_up",
                        "_wake",
                        "_park",
                    }:
                        self.last_motion_at = time.monotonic()
                    next_frame = time.monotonic()
                continue
            with self._lock:
                mode, target, offsets = self.mode, self.target.copy(), self._offsets
                sequence, started = self._expression, self._expression_started
                held, previous = self._held_frame, self._frame.copy()
                idle_motion = mode == MotionMode.ENGAGED and self._idle_motion_enabled
                if not idle_motion or time.monotonic() - self._offsets_at > 0.3:
                    offsets = (0.0,) * 6
            if mode not in {MotionMode.PARKED, MotionMode.CAPTURE_FREEZE}:
                try:
                    x, y, z, roll, pitch, yaw = offsets
                    # Small bounded breathing and speech offsets in metres/radians.
                    if idle_motion:
                        z += 0.0015 * math.sin(time.monotonic() * math.pi)
                    offset = create_head_pose(x=x, y=y, z=z, roll=roll, pitch=pitch, yaw=yaw, degrees=False)
                    if sequence is not None:
                        elapsed = time.monotonic() - started
                        self.last_motion_at = time.monotonic()
                        requested = sequence.evaluate(elapsed)
                    else:
                        base = held or Frame(clamp_pose(target), previous.antennas, 0.0)
                        requested = Frame(base.head @ offset, base.antennas, base.body)
                        # Speech follows a held pose too, but never adds an unsafe
                        # offset to a pose already at the edge of motor travel.
                        try:
                            candidate = safe_frame(requested, previous, 0.02)
                            if not reachable_joints(kinematics.ik(candidate.head, body_yaw=candidate.body)):
                                requested = base
                        except ValueError:
                            requested = base
                    frame = safe_frame(requested, previous, 0.02)
                    self.mini.set_target(head=frame.head, antennas=frame.antennas, body_yaw=frame.body)
                    with self._lock:
                        self._frame = frame
                        if (
                            sequence is not None
                            and sequence is self._expression
                            and elapsed >= sequence.duration
                        ):
                            self._expression = None
                            self._held_frame = frame.copy()
                            self.expression_status = "completed"
                    self.last_success = time.monotonic()
                except Exception as exc:
                    self.error = type(exc).__name__
                    self.set_mode(MotionMode.PARKED)
                    if self.on_fault:
                        self.on_fault(self.error)
            next_frame += 0.02
            self._stop.wait(max(0, next_frame - time.monotonic()))
            if time.monotonic() - next_frame > 0.1:
                next_frame = time.monotonic()
