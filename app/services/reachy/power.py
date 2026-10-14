"""Park with torque off and verify the actual daemon state."""

from __future__ import annotations

import asyncio
import math
import time

import numpy as np

from app.services.reachy.daemon_client import stop_move
from app.services.reachy.motion.manager import MotionMode


class RobotPowerManager:
    def __init__(self, motion, daemon, *, sag_delay: float = 5.0) -> None:
        self.motion, self.daemon, self.sag_delay = motion, daemon, sag_delay
        self.parked = False
        self.error: str | None = None

    async def park(self) -> None:
        self.motion.set_mode(MotionMode.PARKED)
        # A daemon-side stop is the backstop if an SDK goto stops completing.
        for attempt in range(2):
            try:
                future = self.motion.enqueue(self._park)
                await asyncio.wait_for(
                    asyncio.shield(asyncio.wrap_future(future)), timeout=16 + self.sag_delay
                )
                self.parked, self.error = True, None
                return
            except Exception as exc:
                self.error = type(exc).__name__
                stop_move(self.motion.mini)
                if attempt:
                    raise RuntimeError("park_failed_safe_hold") from exc

    def _park(self) -> None:
        mini = self.motion.mini
        try:
            mini.goto_sleep()
            before = self.daemon.state()
        finally:
            mini.disable_motors()
        deadline = time.monotonic() + 1
        while self.daemon.motors().get("mode") != "disabled":
            if time.monotonic() >= deadline:
                raise RuntimeError("motor_torque_not_disabled")
            time.sleep(0.05)
        time.sleep(self.sag_delay)
        after = self.daemon.state()
        expected = before.get("head_joints")
        actual = after.get("head_joints")
        if expected is None or actual is None or len(expected) != len(actual) or not actual:
            raise RuntimeError("sleep_joint_verification_unavailable")
        if np.max(np.abs(np.asarray(expected) - np.asarray(actual))) > 0.10:
            raise RuntimeError("sleep_joint_sag_exceeded")
        from reachy_mini.reachy_mini import SLEEP_HEAD_POSE

        with self.motion._lock:
            self.motion.target = SLEEP_HEAD_POSE.copy()
        from reachy_mini.utils.rotation import Rotation

        pose = after.get("head_pose", {})
        if "m" not in pose:
            raise RuntimeError("sleep_pose_verification_unavailable")
        matrix = np.asarray(pose["m"]).reshape(4, 4)
        displacement = np.linalg.norm(matrix[:3, 3] - SLEEP_HEAD_POSE[:3, 3])
        rotation = Rotation.from_matrix(SLEEP_HEAD_POSE[:3, :3].T @ matrix[:3, :3])
        # Rotation.magnitude is not part of every SDK release; trace is stable.
        angle = math.acos(float(np.clip((np.trace(rotation.as_matrix()) - 1) / 2, -1, 1)))
        if displacement > 0.05 or angle > 0.35:
            raise RuntimeError("sleep_pose_tolerance_exceeded")

    async def wake(self, bearing_radians: float = 0.0) -> None:
        await asyncio.wait_for(self.motion.execute(self._wake, bearing_radians), timeout=5)
        self.parked = False
        self.motion.set_mode(MotionMode.ENGAGED)

    def _wake(self, bearing: float) -> None:
        from reachy_mini.reachy_mini import INIT_HEAD_POSE
        from reachy_mini.utils import create_head_pose

        mini = self.motion.mini
        mini.disable_motors()
        mini.enable_motors()
        present = mini.get_current_head_pose()
        if np.linalg.norm(present - INIT_HEAD_POSE) > 0.1:
            mini.wake_up()
        target = create_head_pose(yaw=max(-35, min(35, math.degrees(bearing))), degrees=True)
        mini.goto_target(head=target, duration=0.6, body_yaw=None)
        self.motion.set_tracking_target(target)
