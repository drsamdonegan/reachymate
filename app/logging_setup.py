"""Logging helpers."""

from __future__ import annotations

import logging

from .config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure root logging once."""

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s:%(lineno)d | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
