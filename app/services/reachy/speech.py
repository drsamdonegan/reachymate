"""Speech queue."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.services.system.telemetry import TelemetryBroker


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


class SpeechWorker:
    """Single-flight speech queue."""

    def __init__(
        self,
        *,
        speak_fn: Callable[[str], Awaitable[None] | None],
        telemetry: TelemetryBroker,
    ) -> None:
        self.speak_fn = speak_fn
        self.telemetry = telemetry
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._speaking = False

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="speech-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def submit(self, text: str) -> None:
        await self.queue.put(text)

    def is_busy(self) -> bool:
        return self._speaking or not self.queue.empty()

    async def _run(self) -> None:
        while True:
            text = await self.queue.get()
            self._speaking = True
            self.telemetry.publish("say", {"text": text})
            try:
                await _maybe_await(self.speak_fn(text))
            finally:
                self._speaking = False
