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
        self._lock = threading.Lock()
        self._commands: queue.Queue = queue.Queue(maxsize=32)
        self._stop = threading.Event()
        self.thread = None
        self.in_flight = False
        self.last_motion_at = 0.0
        self.last_success = None
        self.error = None

    def start(self) -> None:
        if self.thread is None:
            self._stop.clear()
            self.thread = threading.Thread(target=self._run, name="motion-arbiter", daemon=True)
            self.thread.start()

    async def stop(self) -> None:
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
            if mode in {MotionMode.PARKED, MotionMode.CAPTURE_FREEZE}:
                self._offsets = (0.0,) * 6

    def set_tracking_target(self, pose) -> None:
        with self._lock:
            if self.mode != MotionMode.CAPTURE_FREEZE:
                self.target = clamp_pose(pose)

    def set_speech_offsets(self, offsets) -> None:
        with self._lock:
            if self.mode == MotionMode.ENGAGED:
                self._offsets = tuple(offsets)

    def reset_speech(self) -> None:
        with self._lock:
            self._offsets = (0.0,) * 6

    def _run(self) -> None:
        from reachy_mini.utils import create_head_pose

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
            if mode not in {MotionMode.PARKED, MotionMode.CAPTURE_FREEZE}:
                try:
                    x, y, z, roll, pitch, yaw = offsets
                    # Small bounded breathing and speech offsets in metres/radians.
                    if mode == MotionMode.ENGAGED:
                        z += 0.0015 * math.sin(time.monotonic() * math.pi)
                    offset = create_head_pose(x=x, y=y, z=z, roll=roll, pitch=pitch, yaw=yaw, degrees=False)
                    self.mini.set_target(head=clamp_pose(target @ offset), body_yaw=None)
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
