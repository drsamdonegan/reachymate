"""Bounded output writer: network event processing never awaits the audio sink."""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np


class PlaybackWriter:
    def __init__(self, sink: Any, *, max_chunks: int = 64) -> None:
        self.sink = sink
        self.queue: asyncio.Queue[tuple[str | None, np.ndarray]] = asyncio.Queue(maxsize=max_chunks)
        self.interrupted: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._clear_lock = asyncio.Lock()
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy or not self.queue.empty()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="realtime-playback-writer")

    def enqueue(self, item_id: str | None, samples: np.ndarray) -> bool:
        if item_id in self.interrupted:
            return False
        self.start()
        try:
            self.queue.put_nowait((item_id, samples))
            return True
        except asyncio.QueueFull:
            return False

    async def clear(self, *, item_id: str | None = None) -> None:
        async with self._clear_lock:
            if item_id:
                self.interrupted.add(item_id)
            task = self._task
            self._task = None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()
            await self.sink.clear_output()

    async def stop(self) -> None:
        await self.clear()
        self.interrupted.clear()

    async def wait_drained(self) -> None:
        await self.queue.join()
        while self.sink.output_active():
            await asyncio.sleep(0.02)

    async def _run(self) -> None:
        while True:
            item_id, samples = await self.queue.get()
            self._busy = True
            try:
                if item_id not in self.interrupted:
                    await self.sink.push_audio(samples)
            finally:
                self._busy = False
                self.queue.task_done()
