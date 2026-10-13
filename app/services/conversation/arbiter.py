"""Serialize controller announcements against conversational model audio."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class SpeechArbiter:
    def __init__(
        self,
        *,
        wait_response: Callable[[], Awaitable[None]],
        interrupt: Callable[[], Awaitable[None]],
        speak: Callable[[str], Awaitable[None]],
        response_active: Callable[[], bool],
        wait_seconds: float = 1.5,
    ) -> None:
        self.wait_response = wait_response
        self.interrupt = interrupt
        self.speak = speak
        self.response_active = response_active
        self.wait_seconds = wait_seconds
        self._lock = asyncio.Lock()
        self._generation = 0
        self._closed = False

    def reopen(self) -> None:
        self._closed = False

    def close(self) -> None:
        self._closed = True
        self._generation += 1

    async def announce(self, text: str) -> None:
        generation = self._generation
        async with self._lock:
            if self._closed or generation != self._generation:
                return
            if self.response_active():
                try:
                    await asyncio.wait_for(self.wait_response(), timeout=self.wait_seconds)
                except TimeoutError:
                    await self.interrupt()
            if self._closed or generation != self._generation:
                return
            await self.speak(text)
