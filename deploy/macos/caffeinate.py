"""Caffeinate is a sibling, so the signed launcher retains TCC responsibility."""

import json
import subprocess
import sys
import time
from pathlib import Path


def main():
    processes = {}
    while True:
        try:
            pid = int(json.loads(Path(sys.argv[1]).read_text())["pid"])
            if pid not in processes:
                processes[pid] = subprocess.Popen(["caffeinate", "-i", "-w", str(pid)])
            processes = {p: child for p, child in processes.items() if child.poll() is None}
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(10)


if __name__ == "__main__":
    main()
