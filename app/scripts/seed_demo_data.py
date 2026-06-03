"""Seed a demo user and token data."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np

from app.config import get_settings
from app.runtime import AppContext


async def _run() -> None:
    context = AppContext(settings=get_settings())
    await context.start()
    try:
        user = context.users.upsert(
            user_id="demo-user",
            full_name="Demo Coworker",
            slack_user_id="UDEMO",
            slack_display_name="demo",
            company="Reachy Lab",
            active=True,
        )
        vector = np.ones(128, dtype=np.float32)
        vector /= np.linalg.norm(vector)
        context.face_store.replace_embeddings(user.id, [(vector.tolist(), 100.0, "demo")])
        context.salto_tokens.save(
            access_token="demo-token",
            refresh_token="demo-refresh",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
        )
        print({"seeded_user_id": user.id, "slack_user_id": user.slack_user_id})
    finally:
        await context.stop()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
