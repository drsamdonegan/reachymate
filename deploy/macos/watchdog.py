"""Restart a stalled kiosk process using a bounded launchctl operation."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def stale(path, *, now=None):
    try:
        beat = json.loads(Path(path).read_text())
        age = (time.time() if now is None else now) - beat["timestamp"]
        return age > 60 or age < 0
    except (OSError, ValueError, KeyError, TypeError):
        return True


def main():
    # Allow the signed launcher time to acquire TCC on its first boot.
    time.sleep(60)
    while True:
        if stale(sys.argv[1]):
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/au.mlai.reachy.bouncer"],
                timeout=15,
                check=False,
            )
        time.sleep(30)


if __name__ == "__main__":
    main()
