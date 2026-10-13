"""Encrypt plaintext or explicitly rewrap legacy purpose-only template ciphertext."""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.storage.db import create_session_factory, init_db, session_scope
from app.storage.encryption import PREFIX, SensitiveDataCipher
from app.storage.repositories import face_embedding_purpose
from app.storage.tables import FaceEmbeddingRecord, SaltoTokenRecord
from app.vision_stack import authorized_vision_settings


def migrate_rows(session, cipher: SensitiveDataCipher, *, rewrap: bool = False, apply: bool = False):
    counts = {"plaintext_face_templates": 0, "legacy_aad_templates": 0, "plaintext_salto_tokens": 0}
    for row in session.scalars(select(FaceEmbeddingRecord)):
        purpose = face_embedding_purpose(row.user_id, row.id)
        value = row.embedding_vector
        encrypted = isinstance(value, str) and value.startswith(PREFIX)
        if encrypted:
            try:
                cipher.decrypt_json(value, purpose=purpose, allow_plaintext=False)
                continue
            except RuntimeError:
                # Only old authenticated ciphertext is eligible for --rewrap;
                # wrong keys and tampered values still fail rather than falling back.
                vector = cipher.decrypt_json(value, purpose="face_embedding", allow_plaintext=False)
                counts["legacy_aad_templates"] += 1
                if not rewrap:
                    continue
        else:
            vector = value
            counts["plaintext_face_templates"] += 1
        if (
            not isinstance(vector, list)
            or not vector
            or any(not math.isfinite(float(number)) for number in vector)
        ):
            raise RuntimeError("Malformed legacy face template; migration stopped without changes")
        if apply:
            row.embedding_vector = cipher.encrypt_json(vector, purpose=purpose)
    for row in session.scalars(select(SaltoTokenRecord)):
        fields = [("access_token", "salto_access_token"), ("refresh_token", "salto_refresh_token")]
        if any(
            getattr(row, name) is not None and not getattr(row, name).startswith(PREFIX) for name, _ in fields
        ):
            counts["plaintext_salto_tokens"] += 1
        if apply:
            for name, purpose in fields:
                value = getattr(row, name)
                if value is not None and not value.startswith(PREFIX):
                    setattr(row, name, cipher.encrypt_text(value, purpose=purpose))
    return counts


def _backup_sqlite(settings: Settings) -> Path | None:
    if not settings.is_sqlite:
        raise RuntimeError("Take a verified database backup before migrating non-SQLite storage")
    source = Path(settings.db_url.removeprefix("sqlite:///"))
    if not source.is_file():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = source.with_suffix(source.suffix + f".{timestamp}.bak")
    # sqlite3.backup includes committed WAL pages; copying only the main file does not.
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with sqlite3.connect(source) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    print({"backup": str(backup)})
    return backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rewrap", action="store_true")
    args = parser.parse_args()
    asyncio.run(_run_authorized(args))


async def _run_authorized(args) -> None:
    async with authorized_vision_settings(Settings()) as settings:
        _migrate_authorized(args, settings)


def _migrate_authorized(args, settings) -> None:
    cipher = SensitiveDataCipher(settings.data_encryption_key)
    if not cipher.enabled:
        raise SystemExit("DATA_ENCRYPTION_KEY is required")
    if args.apply:
        _backup_sqlite(settings)
    factory = create_session_factory(settings)
    init_db(factory)
    with session_scope(factory) as session:
        counts = migrate_rows(session, cipher, rewrap=args.rewrap, apply=args.apply)
    print({**counts, "apply": args.apply, "rewrap": args.rewrap})


if __name__ == "__main__":
    main()
