"""Restart failed workers with bounded backoff and measured health."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

from app.services.system.heartbeat import Heartbeat

logger = logging.getLogger(__name__)


class SupervisedTask:
    def __init__(
        self,
        name: str,
        run: Callable[[], Awaitable[None]],
        *,
        on_fault: Callable[[str], Awaitable[None]] | None = None,
        heartbeat: Heartbeat | None = None,
        initial_backoff: float = 0.5,
        maximum_backoff: float = 30.0,
    ) -> None:
        self.name, self.run = name, run
        self.on_fault = on_fault
        self.heartbeat = heartbeat or Heartbeat()
        self.initial_backoff, self.maximum_backoff = initial_backoff, maximum_backoff
        self.restarts: deque[float] = deque()
        self.task: asyncio.Task | None = None
        self.error = False

    async def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._supervise(), name=self.name)

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _supervise(self) -> None:
        delay = self.initial_backoff
        while True:
            started = time.monotonic()
            try:
                await self.run()
                raise RuntimeError("worker returned unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                now = time.monotonic()
                self.heartbeat.fail(type(exc).__name__)
                self.restarts.append(now)
                while self.restarts and self.restarts[0] < now - 600:
                    self.restarts.popleft()
                self.error = len(self.restarts) > 5
                logger.warning("%s failed: %s", self.name, type(exc).__name__)
                if self.on_fault is not None:
                    try:
                        await self.on_fault("restart_limit" if self.error else type(exc).__name__)
                    except asyncio.CancelledError:
                        raise
                    except Exception as callback_error:
                        self.heartbeat.fail(type(callback_error).__name__)
                        logger.error(
                            "%s recovery callback failed: %s", self.name, type(callback_error).__name__
                        )
                if now - started > 60:
                    delay = self.initial_backoff
                await asyncio.sleep(delay)
                delay = min(self.maximum_backoff, delay * 2)
