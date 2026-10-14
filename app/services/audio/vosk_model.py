"""Process-wide Vosk acoustic model cache."""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=4)
def load_vosk_model(path: str):
    """Load immutable model data once; recognizers keep independent stream state."""

    from vosk import Model, SetLogLevel

    SetLogLevel(-1)
    return Model(path)
