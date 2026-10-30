"""Local presence-based welcomes. No identity matching or retained face images."""

from __future__ import annotations

import asyncio
import math
import time

import numpy as np


class ArrivalGate:
    def __init__(self, *, stable_seconds=0.8, absent_seconds=5.0, cooldown_seconds=15.0):
        self.stable_seconds, self.absent_seconds, self.cooldown_seconds = (
            stable_seconds,
            absent_seconds,
            cooldown_seconds,
        )
        self.since = None
        self.last_seen = -float("inf")
        self.last_welcome = -float("inf")
        self.welcomed = False

    def update(self, present: bool, now: float, *, visible: bool = False) -> bool:
        if not present:
            if visible:
                self.last_seen = now  # Leaving the tracking zone is not a new arrival.
            self.since = None
            if now - self.last_seen >= self.absent_seconds:
                self.welcomed = False
            return False
        self.last_seen = now
        self.since = now if self.since is None else self.since
        return (
            not self.welcomed
            and now - self.since >= self.stable_seconds
            and now - self.last_welcome >= self.cooldown_seconds
        )

    def mark_welcomed(self, now: float) -> None:
        self.welcomed, self.last_welcome = True, now


def visual_target(head, bbox, width: int, height: int, elapsed: float):
    """Small image-error corrections; no assumption of measured camera intrinsics."""
    from reachy_mini.utils import create_head_pose

    if bbox is None:
        current_yaw = math.degrees(math.atan2(head[1, 0], head[0, 0]))
        current_pitch = math.degrees(math.asin(float(np.clip(-head[2, 0], -1, 1))))
        desired_yaw = 25 * math.sin(elapsed * 2 * math.pi / 24)
        yaw = current_yaw + float(np.clip(desired_yaw - current_yaw, -1.2, 1.2))
        pitch = current_pitch + float(np.clip(-current_pitch, -1.0, 1.0))
    else:
        x, y, w, h = bbox
        horizontal = (x + w / 2 - width / 2) / width
        vertical = (y + h / 2 - height / 2) / height
        horizontal = 0 if abs(horizontal) < 0.035 else horizontal
        vertical = 0 if abs(vertical) < 0.04 else vertical
        yaw = math.degrees(math.atan2(head[1, 0], head[0, 0]))
        pitch = math.degrees(math.asin(float(np.clip(-head[2, 0], -1, 1))))
        yaw += float(np.clip(-horizontal * 12, -2, 2))
        pitch += float(np.clip(vertical * 10, -1.5, 1.5))
    return create_head_pose(yaw=float(np.clip(yaw, -35, 35)), pitch=float(np.clip(pitch, -18, 18)))


class EventGreeter:
    def __init__(self, *, observation, follow, welcome, visible=lambda: False):
        self.observation, self.follow, self.welcome = observation, follow, welcome
        self.visible = visible
        self.enabled = False
        self.greeting = "Welcome to the event!"
        self.gate = ArrivalGate()
        self.task = self.greeting_task = None
        self.started_at = 0.0
        self.last_target_at = 0.0
        self.present = False
        self.welcomes = 0
        self.error = None
        self.retry_after = 0.0

    async def start(self, greeting: str):
        await self.stop()
        self.greeting, self.gate = greeting, ArrivalGate()
        self.enabled, self.started_at = True, time.monotonic()
        self.last_target_at = self.started_at
        self.error, self.welcomes, self.retry_after = None, 0, 0.0
        self.task = asyncio.create_task(self._run(), name="event-greeter")

    async def stop(self):
        self.enabled = self.present = False
        tasks = [t for t in (self.task, self.greeting_task) if t and t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.task = self.greeting_task = None

    async def _run(self):
        while self.enabled:
            try:
                now = time.monotonic()
                face = self.observation()
                self.present = bool(face and face.in_zone and now - face.timestamp < 0.4)
                ready = self.gate.update(self.present, now, visible=self.visible())
                if self.present:
                    self.last_target_at = now
                    await self.follow(face.bbox, now - self.started_at)
                elif now - self.last_target_at > 3:
                    await self.follow(None, now - self.started_at)
                if (
                    ready
                    and now >= self.retry_after
                    and (self.greeting_task is None or self.greeting_task.done())
                ):
                    self.gate.mark_welcomed(now)
                    self.greeting_task = asyncio.create_task(self._welcome(), name="arrival-welcome")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = type(exc).__name__
            await asyncio.sleep(0.15)

    async def _welcome(self):
        try:
            if await self.welcome(self.greeting):
                self.welcomes += 1
                self.error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = type(exc).__name__
            self.gate.welcomed = False
            self.retry_after = time.monotonic() + 30

    def diagnostics(self):
        return {
            "enabled": self.enabled,
            "face_present": self.present,
            "welcomes": self.welcomes,
            "state": "following" if self.present else "searching" if self.enabled else "off",
            "greeting": self.greeting,
            "error": self.error,
            "rearm_after_absence_seconds": self.gate.absent_seconds,
        }
