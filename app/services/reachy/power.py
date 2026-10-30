"""Park with torque off and verify the actual daemon state."""

from __future__ import annotations

import asyncio
import logging
import math
import time

import numpy as np

from app.services.reachy.daemon_client import stop_move
from app.services.reachy.motion.manager import MotionMode

logger = logging.getLogger(__name__)


class RobotPowerManager:
    def __init__(self, motion, daemon, *, sag_delay: float = 5.0, joint_sag_limit: float = 0.10) -> None:
        self.motion, self.daemon, self.sag_delay = motion, daemon, sag_delay
        self.joint_sag_limit = joint_sag_limit
        self.parking = False
        self._transition_lock = asyncio.Lock()
        self.parked = False
        self.error: str | None = None

    async def park(self) -> None:
        async with self._transition_lock:
            self.parking = True
            try:
                await self._park_attempts()
            finally:
                self.parking = False

    async def _park_attempts(self) -> None:
        self.parked = False
        self.motion.set_mode(MotionMode.PARKED)
        # A daemon-side stop is the backstop if an SDK goto stops completing.
        for attempt in range(2):
            try:
                future = self.motion.enqueue(self._park)
                await asyncio.wait_for(
                    asyncio.shield(asyncio.wrap_future(future)), timeout=16 + self.sag_delay
                )
                self.parked, self.error = True, None
                self.motion.error = None
                return
            except Exception as exc:
                self.error = type(exc).__name__
                logger.warning("Park attempt %s failed: %s", attempt + 1, exc)
                stop_move(self.motion.mini)
                if attempt:
                    raise RuntimeError("park_failed_safe_hold") from exc

    def _park(self) -> None:
        mini = self.motion.mini
        try:
            # SDK goto_sleep sends targets but does not enable torque. Startup
            # inherits disabled motors from the daemon's no-wake profile.
            mini.enable_motors()
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
        sag = float(np.max(np.abs(np.asarray(expected) - np.asarray(actual))))
        logger.info(
            "Sleep verification: torque disabled, maximum joint sag %.4f radians (limit %.4f)",
            sag,
            self.joint_sag_limit,
        )
        if sag > self.joint_sag_limit:
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
        async with self._transition_lock:
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
        rebase = getattr(self.motion, "rebase_frame", None)
        if rebase:
            rebase(target)
