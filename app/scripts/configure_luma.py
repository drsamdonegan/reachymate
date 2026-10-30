"""Validate and save a Luma key from a masked, local Terminal prompt."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values, set_key
from pydantic import SecretStr

from app.config import BASE_DIR
from app.services.events.luma import LumaClient, LumaError
from app.services.events.qr import EVENT_ID


def save_credentials(path: Path, key: SecretStr, event_id: str) -> None:
    """Keep unrelated settings intact and publish the new secret atomically at mode 0600."""
    if path.is_symlink():
        raise ValueError("Use a regular local .env file, not a symbolic link")
    content = path.read_text() if path.exists() else ""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".luma-", delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
        set_key(str(temporary), "LUMA_API_KEY", key.get_secret_value())
        set_key(str(temporary), "LUMA_EVENT_ID", event_id)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def configure(path: Path, key: SecretStr, event_id: str, *, client_factory=LumaClient) -> None:
    if not EVENT_ID.fullmatch(event_id):
        raise ValueError("Select a Luma event ID (evt-...) before configuring its key")
    value = key.get_secret_value()
    if not value or len(value) > 1024 or any(ord(c) <= 32 or ord(c) > 126 for c in value):
        raise ValueError("The API key must be a non-empty value without spaces or line breaks")
    client = client_factory(key)
    try:
        await client.event(event_id)
    finally:
        await client.close()
    save_credentials(path, key, event_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=BASE_DIR / ".env")
    parser.add_argument("--event-id", help="Defaults to LUMA_EVENT_ID in the local .env")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        print(
            "Run this command in Terminal so the API key can be entered without echoing it.", file=sys.stderr
        )
        return 2
    try:
        event_id = args.event_id or dotenv_values(args.env_file).get("LUMA_EVENT_ID") or ""
        if not EVENT_ID.fullmatch(event_id):
            raise ValueError("Set LUMA_EVENT_ID in .env or pass --event-id evt-...")
        print("The key is sent only to Luma for a read-only event access check, then saved locally.")
        key = SecretStr(getpass.getpass("Luma API key (hidden): ").strip())
        asyncio.run(configure(args.env_file, key, event_id))
    except (KeyboardInterrupt, EOFError):
        print("Cancelled; no key was saved.")
        return 1
    except (LumaError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Could not save the local configuration; check file permissions.", file=sys.stderr)
        return 1
    print("Luma access verified and key saved privately. Restart Reachy to load it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
