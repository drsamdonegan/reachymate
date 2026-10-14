"""Render installation-specific LaunchAgents; no literal template paths reach launchd."""

from __future__ import annotations

import argparse
import plistlib
from pathlib import Path


def agents(root: Path, logs: Path):
    common = {
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "ExitTimeOut": 30,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "OPENCV_AVFOUNDATION_SKIP_AUTH": "1",
            "HOST": "127.0.0.1",
            "PHYSICAL_UNLOCK_ENABLED": "false",
            "ACCESS_MODE": "shadow",
            "DOOR_STAGE": "0",
        },
    }
    commands = {
        "daemon": [
            str(root / ".venv-daemon/bin/python"),
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
        ],
        "bouncer": [
            str(root / "deploy/macos/build/Reachy Concierge.app/Contents/MacOS/ReachyConcierge"),
            str(root / ".venv/bin/python"),
            str(root),
        ],
        "watchdog": [
            str(root / ".venv/bin/python"),
            str(root / "deploy/macos/watchdog.py"),
            str(root / "data/heartbeat.json"),
        ],
        "caffeinate": [
            str(root / ".venv/bin/python"),
            str(root / "deploy/macos/caffeinate.py"),
            str(root / "data/heartbeat.json"),
        ],
    }
    return {
        name: dict(
            common,
            Label=f"au.mlai.reachy.{name}",
            ProgramArguments=arguments,
            StandardOutPath=str(logs / f"{name}.log"),
            StandardErrorPath=str(logs / f"{name}.error.log"),
        )
        for name, arguments in commands.items()
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logs = args.root / ".codex-runtime-logs"
    logs.mkdir(exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, agent in agents(args.root.resolve(), logs.resolve()).items():
        with (args.output / f"au.mlai.reachy.{name}.plist").open("wb") as file:
            plistlib.dump(agent, file)


if __name__ == "__main__":
    main()
