"""Explicitly opted-in motion sequence, ending parked with torque disabled."""

from __future__ import annotations

import asyncio
import time

from app.scripts.hw.common import parser, write_report
from app.services.reachy.daemon_client import DaemonClient, stop_move
from app.services.reachy.motion.manager import MotionManager
from app.services.reachy.power import RobotPowerManager


async def probe(args):
    if not args.execute:
        raise RuntimeError("Motion probe requires --execute and an attended clear robot")
    from reachy_mini import ReachyMini
    from reachy_mini.utils import create_head_pose

    daemon = DaemonClient()
    await asyncio.to_thread(daemon.wait_ready)
    mini = ReachyMini(
        host="127.0.0.1", connection_mode="network", media_backend="no_media", automatic_body_yaw=False
    )
    motion = MotionManager(mini)
    motion.start()
    power = RobotPowerManager(motion, daemon)
    measurements = {}
    try:
        await power.wake()
        for yaw in (-20, 20):
            await motion.execute(
                mini.goto_target, head=create_head_pose(yaw=yaw), duration=0.6, body_yaw=None
            )
        start = time.monotonic()
        long_goto = motion.enqueue(mini.goto_target, head=create_head_pose(), duration=8, body_yaw=None)
        await asyncio.sleep(0.5)
        await asyncio.to_thread(stop_move, mini)
        try:
            await asyncio.wait_for(asyncio.wrap_future(long_goto), 2)
            measurements["stop_completed_seconds"] = time.monotonic() - start
        except Exception as exc:
            measurements["stop_error"] = type(exc).__name__
        await power.park()
        measurements.update({"motors": daemon.motors(), "state": daemon.state(), "status": "measured"})
        write_report(args.output, measurements)
    finally:
        try:
            await power.park()
        finally:
            mini.disable_motors()
            await motion.stop()
            mini.client.disconnect()
            daemon.close()


def main():
    cli = parser("motion_probe")
    cli.add_argument("--execute", action="store_true")
    asyncio.run(probe(cli.parse_args()))


if __name__ == "__main__":
    main()
