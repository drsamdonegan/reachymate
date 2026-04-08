"""CLI enrollment helper."""

from __future__ import annotations

import argparse
import asyncio
import base64
from pathlib import Path

from app.config import get_settings
from app.runtime import AppContext


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enroll a user from image files")
    parser.add_argument("--full-name", required=True)
    parser.add_argument("--slack-user-id", required=True)
    parser.add_argument("--slack-display-name", required=True)
    parser.add_argument("--company")
    parser.add_argument("images", nargs="+", help="Image files for enrollment")
    return parser.parse_args()


async def _run() -> None:
    args = parse_args()
    settings = get_settings()
    context = AppContext(settings=settings)
    await context.start()
    try:
        payload = {
            "full_name": args.full_name,
            "slack_user_id": args.slack_user_id,
            "slack_display_name": args.slack_display_name,
            "company": args.company,
            "samples": [
                {
                    "label": Path(path).stem,
                    "image_b64": base64.b64encode(Path(path).read_bytes()).decode("utf-8"),
                }
                for path in args.images
            ],
        }
        result = await context.enroll(payload)
        print(result)
    finally:
        await context.stop()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
