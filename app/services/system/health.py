"""Fail-closed component readiness rules."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from app.models.enums import HealthStatus

ACCESS_REQUIRED_COMPONENTS = (
    "database",
    "booking",
    "door_action",
    "camera",
    "face_identity",
    "face_liveness",
)
CONVERSATION_REQUIRED_COMPONENTS = (
    "robot",
    "audio",
    "local_wake",
    "realtime",
    "public_knowledge",
)


def components_ready(
    snapshot: Mapping[str, HealthStatus],
    required: Iterable[str],
) -> bool:
    """Require every named component to be present and exactly healthy."""

    return all(snapshot.get(component) == HealthStatus.HEALTHY for component in required)
