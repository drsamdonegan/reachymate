"""Print recent audio diagnostics."""

from __future__ import annotations

import argparse
import asyncio

from app.config import get_settings
from app.runtime import AppContext


async def _run() -> None:
    parser = argparse.ArgumentParser(description="Read live audio diagnostics")
    parser.add_argument("--seconds", type=int, default=10)
    args = parser.parse_args()

    context = AppContext(settings=get_settings())
    await context.start()
    try:
        for _ in range(args.seconds):
            print(context.audio_diagnostics())
            await asyncio.sleep(1)
    finally:
        await context.stop()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
