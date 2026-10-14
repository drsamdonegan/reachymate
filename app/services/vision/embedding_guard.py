"""Explicit local authorization before generating any biometric probe.

Context variables are copied by asyncio.to_thread; detection-only tracking
never enters this scope. Each scoped probe is discarded after its attempt.
"""

from __future__ import annotations

import threading
from collections import Counter
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Literal

import numpy as np


@dataclass(frozen=True)
class _Authorization:
    attempt_id: str
    purpose: str


_authorization: ContextVar[_Authorization | None] = ContextVar("embedding_authorization", default=None)
_counter = Counter()
_counter_lock = threading.Lock()


class EmbeddingGuard:
    @staticmethod
    def record_embedding() -> None:
        scope = EmbeddingGuard.require_scope()
        with _counter_lock:
            _counter[scope.purpose] += 1

    @staticmethod
    def counters() -> dict[str, int]:
        with _counter_lock:
            return dict(_counter)

    @staticmethod
    def active() -> bool:
        return _authorization.get() is not None

    @staticmethod
    def require_scope() -> _Authorization:
        value = _authorization.get()
        if value is None:
            with _counter_lock:
                _counter["outside_scope_attempts"] += 1
            raise RuntimeError("Biometric embedding requires an authorized local scope")
        return value


class AccessAttemptScope:
    def __init__(self, attempt_id: str, purpose: Literal["access", "enrollment", "evaluation"] = "access"):
        if not attempt_id or purpose not in {"access", "enrollment", "evaluation"}:
            raise ValueError("A scoped attempt id and valid purpose are required")
        self.value = _Authorization(attempt_id, purpose)
        self._token: Token | None = None

    def __enter__(self):
        self._token = _authorization.set(self.value)
        return self

    def __exit__(self, *_):
        if self._token is not None:
            _authorization.reset(self._token)
            self._token = None


def zeroize(value) -> None:
    """Overwrite owned mutable buffers; never retain a probe in an audit row."""
    if isinstance(value, np.ndarray):
        value.fill(0)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, (list, np.ndarray)):
                zeroize(item)
            else:
                value[index] = 0.0
