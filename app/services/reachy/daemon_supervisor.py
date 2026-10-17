"""Probe daemon/app lock and recover orphaned daemon SDK moves."""

from __future__ import annotations

import asyncio
import time

import httpx

from app.services.reachy.daemon_client import stop_move
from app.services.system.heartbeat import Heartbeat
from app.services.system.supervisor import SupervisedTask


class DaemonSupervisor:
    def __init__(self, daemon, motion, *, on_fault=None, restart_fallback=None) -> None:
        self.daemon, self.motion, self.on_fault, self.restart_fallback = (
            daemon,
            motion,
            on_fault,
            restart_fallback,
        )
        self.heartbeat = Heartbeat(10)
        self.worker = SupervisedTask("daemon-supervisor", self._run, heartbeat=self.heartbeat)
        self._down_since = self._stuck_since = None

    async def start(self):
        await self.worker.start()

    async def stop(self):
        await self.worker.stop()

    async def _restart(self):
        try:
            await asyncio.to_thread(self.daemon.restart)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return  # Daemon already has a restart/start/stop job in progress.
            if self.restart_fallback:
                await self.restart_fallback()
            else:
                raise
        except httpx.HTTPError:
            if self.restart_fallback:
                await self.restart_fallback()
            else:
                raise

    async def probe(self):
        now = time.monotonic()
        try:
            status = await asyncio.to_thread(self.daemon.status)
            if status.get("state") != "running":
                raise RuntimeError("daemon_not_running")
            lock = await asyncio.to_thread(self.daemon.app_lock)
            if lock.get("state") != "free":
                from app.services.reachy.motion.manager import MotionMode

                self.motion.set_mode(MotionMode.PARKED)
                await self.motion.execute(self.motion.mini.disable_motors)
                if self.on_fault:
                    await self.on_fault("robot_app_lock_busy")
                self.heartbeat.fail("robot_app_lock_busy")
                return
            self._down_since = None
            if not self.motion.in_flight:
                frame = getattr(self.motion, "frame", None)
                target = frame().head if frame else self.motion.target.copy()
                result = await self.motion.execute(self.daemon.probe_target, target)
                if result.get("reason") == "move_running":
                    self._stuck_since = self._stuck_since or now
                    if now - self._stuck_since >= 5:
                        await asyncio.to_thread(stop_move, self.motion.mini)
                        result = await self.motion.execute(
                            self.daemon.probe_target, target
                        )
                        if result.get("reason") == "move_running":
                            await self._restart()
                        self._stuck_since = None
                else:
                    self._stuck_since = None
            self.heartbeat.beat()
        except (httpx.HTTPError, RuntimeError):
            self.heartbeat.fail("daemon_probe_failed")
            self._down_since = self._down_since or now
            if now - self._down_since >= 45:
                await self._restart()
                self._down_since = now

    async def _run(self):
        while True:
            await self.probe()
            await asyncio.sleep(10)
