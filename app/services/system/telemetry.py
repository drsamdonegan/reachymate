"""In-memory telemetry fanout."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any


class TelemetryBroker:
    """Simple pub/sub for the local UI."""

    def __init__(self, history_size: int = 200) -> None:
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        event = {
            "kind": kind,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        self._history.append(event)
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for queue in self._subscribers:
            try:
                if queue.full():
                    _ = queue.get_nowait()
                queue.put_nowait(event)
            except Exception:
                dead.append(queue)
        for queue in dead:
            self._subscribers.discard(queue)

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        for event in self.snapshot():
            await queue.put(event)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._history)
