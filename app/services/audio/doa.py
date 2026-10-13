"""Local DoA ring, read even while the head is dormant."""

from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass

from app.services.system.heartbeat import Heartbeat
from app.services.system.supervisor import SupervisedTask


@dataclass(frozen=True)
class DoaSample:
    theta: float
    speech: bool
    timestamp: float

    @property
    def bearing(self) -> float:
        return math.pi / 2 - self.theta


class DoaMonitor:
    def __init__(self, controller, *, motion_time=lambda: 0.0) -> None:
        self.controller, self.motion_time = controller, motion_time
        self.samples: deque[DoaSample] = deque(maxlen=20)
        self.heartbeat = Heartbeat(0.1)
        self.worker = SupervisedTask("doa-monitor", self._run, heartbeat=self.heartbeat)

    async def start(self) -> None:
        await asyncio.to_thread(self.controller.open)
        await self.worker.start()

    async def stop(self) -> None:
        await self.worker.stop()
        await asyncio.to_thread(self.controller.close)
        self.samples.clear()

    async def _run(self) -> None:
        await asyncio.to_thread(self.controller.open)
        while True:
            value = await asyncio.to_thread(self.controller.read_doa)
            now = time.monotonic()
            if value is None:
                await asyncio.to_thread(self.controller.close)
                raise RuntimeError("doa_unavailable")
            theta, speech = value
            if math.isfinite(theta) and 0 <= theta <= math.pi and now - self.motion_time() >= 0.6:
                self.samples.append(DoaSample(float(theta), bool(speech), now))
            self.heartbeat.beat()
            await asyncio.sleep(0.1)

    def latest(self) -> DoaSample | None:
        return self.samples[-1] if self.samples else None

    def wake_bearing(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        recent = [s for s in list(self.samples)[-5:] if now - s.timestamp < 0.6 and s.speech]
        if len(recent) < 3:
            return 0.0
        return max(
            -math.radians(35), min(math.radians(35), math.pi / 2 - statistics.median(s.theta for s in recent))
        )
