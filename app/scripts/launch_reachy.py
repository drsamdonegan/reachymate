"""Start or reuse the local USB bench stack, connect GPT-Live and open its dashboard."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.scripts.run_reachy_live_test import LIVE_TEST_DEFAULTS

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_URL = "http://127.0.0.1:8080"


class LaunchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Service:
    name: str
    port: int
    path: str
    identity: str
    arguments: tuple[str, ...]


SERVICES = (
    Service(
        "daemon",
        8000,
        "/api/daemon/status",
        "daemon_status",
        (
            "-m",
            "app.scripts.run_reachy_daemon",
            "--no-media",
            "--no-wake-up-on-start",
            "--goto-sleep-on-stop",
            "--fastapi-host",
            "127.0.0.1",
            "--dataset-update-interval",
            "0",
            "--no-preload-datasets",
        ),
    ),
    Service(
        "bookings",
        8091,
        "/openapi.json",
        "Mock mlai-backend",
        ("-m", "uvicorn", "app.scripts.mock_mlai_backend:app", "--host", "127.0.0.1", "--port", "8091"),
    ),
    Service(
        "frontdesk",
        8092,
        "/openapi.json",
        "Mock MLAI front desk",
        ("-m", "app.scripts.mock_frontdesk"),
    ),
    Service(
        "app",
        8080,
        "/openapi.json",
        "Reachy Office Bouncer",
        ("-m", "app.scripts.run_reachy_live_test", "--demo", "--auto-chat"),
    ),
)


def request_json(url: str, *, method: str = "GET", timeout: float = 3) -> dict:
    # Ignore proxy environment variables for these loopback-only service requests.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, method=method)
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def probe_service(service: Service) -> bool:
    try:
        payload = request_json(f"http://127.0.0.1:{service.port}{service.path}")
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        raise LaunchError(f"Unexpected service response on port {service.port}.")
    identity = payload.get("type") if service.name == "daemon" else payload.get("info", {}).get("title")
    if identity != service.identity:
        raise LaunchError(f"Port {service.port} belongs to another service. Close it before starting Reachy.")
    if service.name == "daemon":
        if not payload.get("no_media") or payload.get("simulation_enabled"):
            raise LaunchError(
                "The existing daemon must use --no-media with real USB hardware, not simulation."
            )
        backend = payload.get("backend_status") or {}
        return payload.get("state") == "running" and backend.get("ready") is True and not backend.get("error")
    return True


@contextmanager
def launch_lock(logs: Path, *, timeout: float = 240):
    """Serialize double clicks so only one launcher creates the hardware owners."""
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (logs / "launcher.lock").open("a") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LaunchError("Another Reachy launcher is still starting. Check its Terminal window.")
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def spawn_service(service: Service, root: Path, logs: Path) -> subprocess.Popen:
    log_path = logs / f"launcher-{service.name}.log"
    with log_path.open("ab", buffering=0) as output:
        log_path.chmod(0o600)
        process = subprocess.Popen(
            [str(root / ".venv/bin/python"), *service.arguments],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, **LIVE_TEST_DEFAULTS) if service.name == "app" else None,
            # macOS posix_spawn preserves the launching Terminal's TCC permissions.
            close_fds=False,
            start_new_session=True,
        )
    if sys.platform == "darwin" and service.name in {"daemon", "app"}:
        subprocess.Popen(
            ["/usr/bin/caffeinate", "-i", "-w", str(process.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=False,
            start_new_session=True,
        )
    return process


def ensure_service(service: Service, root: Path, logs: Path, *, timeout: float = 60) -> None:
    if probe_service(service):
        print(f"Reusing Reachy {service.name} on {service.port}.", flush=True)
        return
    process = None
    if not port_open(service.port):
        print(f"Starting Reachy {service.name}…", flush=True)
        process = spawn_service(service, root, logs)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise LaunchError(f"{service.name} exited. Check {logs / f'launcher-{service.name}.log'}.")
        if probe_service(service):
            return
        time.sleep(0.4)
    hint = (
        " Check USB and external power; inspect the daemon log for motor faults."
        if service.name == "daemon"
        else ""
    )
    raise LaunchError(f"{service.name} is not ready on port {service.port}.{hint} Logs: {logs}")


def connect_chat() -> bool:
    diagnostics = request_json(f"{DASHBOARD_URL}/api/admin/diagnostics/audio")
    conversation = diagnostics.get("conversation", {})
    voice = diagnostics.get("voice", {})
    if conversation.get("pause_reason") == "luma_express":
        print("Luma camera handoff is active; leaving voice and camera paused.", flush=True)
        return False
    if not (
        voice.get("api") == "gpt_live"
        and conversation.get("attended_available")
        and conversation.get("dashboard_auto_start")
        and voice.get("vision", {}).get("enabled")
    ):
        raise LaunchError(
            "An older or different app is already running on 8080. Stop it in its Terminal, "
            "then reopen Start Reachy.command to enable GPT-Live, vision and dashboard auto chat."
        )
    result = request_json(f"{DASHBOARD_URL}/api/admin/test-conversation", method="POST", timeout=45)
    if not result.get("running"):
        raise LaunchError(
            "GPT-Live did not connect. Open the dashboard for voice diagnostics and Start chat."
        )
    return True


def launch(root: Path = ROOT, *, open_browser: bool = True) -> None:
    logs = root / ".codex-runtime-logs"
    with launch_lock(logs):
        for service in SERVICES:
            ensure_service(service, root, logs)
        try:
            chat_active = connect_chat()
        finally:
            # Leave a usable diagnostics window even when cloud voice cannot connect.
            if open_browser:
                webbrowser.open(f"{DASHBOARD_URL}/")
    print(f"Reachy is ready: {DASHBOARD_URL}/", flush=True)
    if chat_active:
        print("Microphone live to OpenAI. Camera snapshots are sent only on visual questions.")
        print("Stop chat ends the session; reopening the dashboard starts it again (ten-minute cap).")
    else:
        print("Luma owns the camera. Close its scanner, then return the camera from the dashboard.")
    print("Services stay running when you close this Terminal. Door actions remain shadow-only.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-browser", action="store_true", help="Connect chat without opening a browser tab"
    )
    args = parser.parse_args()
    try:
        launch(open_browser=not args.no_browser)
    except (LaunchError, OSError, ValueError) as exc:
        # Provider bodies and keys are deliberately excluded from launcher errors.
        print(f"Reachy could not start: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
