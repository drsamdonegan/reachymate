from __future__ import annotations

import asyncio

import pytest

from app.services.system.telemetry import TelemetryBroker


@pytest.mark.asyncio
async def test_dashboard_reconnect_with_full_history_never_blocks_and_stays_bounded():
    broker = TelemetryBroker(history_size=200)
    for index in range(250):
        broker.publish("test", {"index": index})
    queue = await asyncio.wait_for(broker.subscribe(), 1)
    assert queue.qsize() == queue.maxsize == 100
    assert queue.get_nowait()["payload"]["index"] == 150
    for index in range(250, 450):
        broker.publish("test", {"index": index})
    assert queue.qsize() == 100
    assert queue.get_nowait()["payload"]["index"] == 350
    broker.unsubscribe(queue)
    assert not broker._subscribers
