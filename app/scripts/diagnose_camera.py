"""Print current camera diagnostics."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.config import get_settings
from app.runtime import AppContext


async def _run() -> None:
    parser = argparse.ArgumentParser(description="Inspect current camera state")
    parser.add_argument("--snapshot-out")
    args = parser.parse_args()

    context = AppContext(settings=get_settings())
    await context.start()
    try:
        print(context.camera_diagnostics())
        if args.snapshot_out:
            snapshot = context.get_camera_snapshot()
            if snapshot is not None:
                Path(args.snapshot_out).write_bytes(snapshot)
                print({"snapshot_written": args.snapshot_out})
    finally:
        await context.stop()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
