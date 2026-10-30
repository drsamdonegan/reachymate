import asyncio
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from app.services.reachy.power import RobotPowerManager


@pytest.mark.asyncio
async def test_parallel_park_requests_are_serialized_and_report_in_progress():
    power = RobotPowerManager(None, None)
    active = 0
    maximum = 0

    async def park_attempts():
        nonlocal active, maximum
        assert power.parking
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1

    power._park_attempts = park_attempts
    await asyncio.gather(power.park(), power.park())
    assert maximum == 1
    assert not power.parking


@pytest.mark.asyncio
async def test_successful_park_retry_clears_transient_motion_fault(monkeypatch):
    count = 0

    def enqueue(_function):
        nonlocal count
        count += 1
        future = Future()
        if count == 1:
            future.set_exception(RuntimeError("sleep_joint_sag_exceeded"))
        else:
            future.set_result(None)
        return future

    motion = SimpleNamespace(enqueue=enqueue, set_mode=lambda _mode: None, mini=None, error="RuntimeError")
    monkeypatch.setattr("app.services.reachy.power.stop_move", lambda _mini: None)
    power = RobotPowerManager(motion, None)
    await power.park()
    assert count == 2
    assert power.parked
    assert power.error is None
    assert motion.error is None
