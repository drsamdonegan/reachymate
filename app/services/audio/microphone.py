"""Microphone adapters."""

from __future__ import annotations

import asyncio
import queue
from collections.abc import Awaitable, Callable
from typing import Protocol

from app.config import Settings


class MicrophoneAdapter(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def read_chunk(self) -> bytes: ...


class NullMicrophoneAdapter:
    """Simulation microphone that yields silence."""

    def __init__(self, settings: Settings) -> None:
        self.chunk_bytes = int(settings.sample_rate_hz * (settings.wakeword_chunk_ms / 1000.0) * 2)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def read_chunk(self) -> bytes:
        await asyncio.sleep(0.08)
        return b"\x00" * self.chunk_bytes


class SoundDeviceMicrophoneAdapter:
    """Laptop microphone via sounddevice."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._queue: queue.Queue[bytes] = queue.Queue()
        self._stream = None
        self.chunk_frames = int(settings.sample_rate_hz * (settings.wakeword_chunk_ms / 1000.0))

    async def start(self) -> None:
        import sounddevice as sd

        def callback(indata, frames, _time, status) -> None:  # type: ignore[no-untyped-def]
            if status:
                return
            self._queue.put(bytes(indata))

        self._stream = sd.RawInputStream(
            samplerate=self.settings.sample_rate_hz,
            blocksize=self.chunk_frames,
            channels=1,
            dtype="int16",
            callback=callback,
            device=self.settings.microphone_device,
        )
        self._stream.start()

    async def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def read_chunk(self) -> bytes:
        return await asyncio.to_thread(self._queue.get)
