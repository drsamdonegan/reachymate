"""Speech queue."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from app.services.system.telemetry import TelemetryBroker


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class SpeechWorker:
    """Single-flight speech queue."""

    def __init__(
        self,
        *,
        speak_fn: Callable[[str], Awaitable[None] | None],
        telemetry: TelemetryBroker,
        item_timeout_seconds: float = 30.0,
    ) -> None:
        self.speak_fn = speak_fn
        self.telemetry = telemetry
        self.item_timeout_seconds = item_timeout_seconds
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self._task: asyncio.Task[None] | None = None
        self._speaking = False

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="speech-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.drop_pending()
        self._speaking = False

    async def submit(self, text: str) -> None:
        await self.queue.put(text)

    def drop_pending(self) -> None:
        """Discard visitor-scoped announcements at the session boundary."""

        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self.queue.task_done()

    async def wait_idle(self) -> None:
        await self.queue.join()

    def is_busy(self) -> bool:
        return self._speaking or not self.queue.empty()

    async def _run(self) -> None:
        while True:
            text = await self.queue.get()
            self._speaking = True
            self.telemetry.publish("say", {"characters": len(text), "content_retained": False})
            try:
                await asyncio.wait_for(_maybe_await(self.speak_fn(text)), self.item_timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.telemetry.publish("speech", {"event": "speech_failed", "error": type(exc).__name__})
            finally:
                self._speaking = False
                self.queue.task_done()
