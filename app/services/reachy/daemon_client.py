"""Loopback-only daemon REST; SDK task stop is deliberately not REST move/stop."""

from __future__ import annotations

import ipaddress
import time

import httpx


class DaemonClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8000, *, client=None) -> None:
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError("Daemon must be bound to loopback")
        self.base_url = f"http://{'[' + host + ']' if ':' in host else host}:{port}/api"
        self.client = client or httpx.Client(base_url=self.base_url, timeout=3.0, trust_env=False)

    def request(self, method: str, path: str, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def status(self):
        return self.request("GET", "/daemon/status")

    def app_lock(self):
        return self.request("GET", "/daemon/robot-app-lock-status")

    def motors(self):
        return self.request("GET", "/motors/status")

    def state(self):
        return self.request(
            "GET",
            "/state/full",
            params={
                "with_head_joints": "true",
                "with_target_head_joints": "true",
                "with_head_pose": "true",
                "use_pose_matrix": "true",
            },
        )

    def probe_target(self, target):
        return self.request(
            "POST", "/move/set_target", json={"target_head_pose": {"m": target.reshape(-1).tolist()}}
        )

    def restart(self):
        return self.request("POST", "/daemon/restart")

    def wait_ready(self, timeout: float = 8) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                status = self.status()
                if status.get("state") in {"running", "ready"} or status.get("status") in {
                    "running",
                    "ready",
                }:
                    if self.app_lock().get("state") not in {None, "free"}:
                        raise RuntimeError("robot_app_lock_busy")
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Reachy daemon is not ready")
            time.sleep(0.25)

    def close(self):
        self.client.close()


def stop_move(mini) -> None:
    from reachy_mini.io.protocol import StopMoveCmd

    mini.client.send_command(StopMoveCmd())
