"""Revoke, delete or export an enrolled member through a standalone local DB session."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from app.config import get_settings
from app.storage.db import create_session_factory, init_db, session_scope
from app.storage.repositories import UserRepository
from app.vision_stack import authorized_vision_settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["revoke", "delete", "export", "end-membership", "reinstate"])
    parser.add_argument("user_id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "export" and args.output is None:
        parser.error("export requires --output; private member records are never printed")
    asyncio.run(_run_authorized(args))


async def _run_authorized(args) -> None:
    # Rights withdrawal and deletion must work even when the KEK is unreachable.
    async with authorized_vision_settings(get_settings(), require_key=args.action == "export") as settings:
        _manage_authorized(args, settings)


def _manage_authorized(args, settings) -> None:
    factory = create_session_factory(settings)
    init_db(factory)
    with session_scope(factory) as session:
        repo = UserRepository(session)
        if args.action == "export":
            payload = json.dumps(repo.export(args.user_id), default=str, indent=2)
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
        elif args.action == "revoke":
            repo.revoke(args.user_id)
        elif args.action == "end-membership":
            repo.end_membership(args.user_id)
        elif args.action == "reinstate":
            repo.reinstate(args.user_id)
        else:
            repo.delete(args.user_id)
    print({"action": args.action, "completed": True})


if __name__ == "__main__":
    main()
