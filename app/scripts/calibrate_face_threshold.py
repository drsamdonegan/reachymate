"""Estimate a face match threshold from local embeddings."""

from __future__ import annotations

import argparse
import asyncio
from itertools import combinations

import numpy as np

from app.config import get_settings
from app.runtime import AppContext


async def _run() -> None:
    parser = argparse.ArgumentParser(description="Suggest a face match threshold from enrolled embeddings")
    _ = parser.parse_args()
    context = AppContext(settings=get_settings())
    await context.start()
    try:
        embeddings = context.face_store.list_embeddings()
        same_scores: list[float] = []
        diff_scores: list[float] = []
        for vectors in embeddings.values():
            for left, right in combinations(vectors, 2):
                same_scores.append(float(np.dot(np.array(left), np.array(right))))
        user_items = list(embeddings.items())
        for idx, (_, left_vectors) in enumerate(user_items):
            for _, right_vectors in user_items[idx + 1 :]:
                if left_vectors and right_vectors:
                    diff_scores.append(float(np.dot(np.array(left_vectors[0]), np.array(right_vectors[0]))))

        if not same_scores or not diff_scores:
            print("Need at least two users with multiple embeddings to calibrate.")
            return

        suggested = (min(same_scores) + max(diff_scores)) / 2
        print(
            {
                "same_min": round(min(same_scores), 4),
                "same_max": round(max(same_scores), 4),
                "diff_min": round(min(diff_scores), 4),
                "diff_max": round(max(diff_scores), 4),
                "suggested_threshold": round(suggested, 4),
            }
        )
    finally:
        await context.stop()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
