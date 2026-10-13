from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.enums import HealthStatus
from app.services.system.health import ACCESS_REQUIRED_COMPONENTS, components_ready
from app.storage.db import create_session_factory, init_db, session_scope
from app.storage.repositories import SystemHealthRepository
from app.storage.tables import SystemHealthRecord


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"database": HealthStatus.HEALTHY},
        {name: HealthStatus.HEALTHY for name in ACCESS_REQUIRED_COMPONENTS if name != "camera"},
        {
            **{name: HealthStatus.HEALTHY for name in ACCESS_REQUIRED_COMPONENTS},
            "camera": HealthStatus.DEGRADED,
        },
        {
            **{name: HealthStatus.HEALTHY for name in ACCESS_REQUIRED_COMPONENTS},
            "booking": HealthStatus.ERROR,
        },
    ],
)
def test_access_readiness_fails_closed(snapshot) -> None:  # type: ignore[no-untyped-def]
    assert components_ready(snapshot, ACCESS_REQUIRED_COMPONENTS) is False


def test_access_readiness_requires_all_components_healthy() -> None:
    snapshot = {name: HealthStatus.HEALTHY for name in ACCESS_REQUIRED_COMPONENTS}

    assert components_ready(snapshot, ACCESS_REQUIRED_COMPONENTS) is True


def test_stale_component_is_reported_as_error(settings) -> None:
    session_factory = create_session_factory(settings)
    init_db(session_factory)
    with session_scope(session_factory) as session:
        repository = SystemHealthRepository(session)
        repository.set_status("database", HealthStatus.HEALTHY)
        record = session.get(SystemHealthRecord, "database")
        assert record is not None
        record.updated_at = datetime.now(timezone.utc) - timedelta(seconds=60)

    with session_scope(session_factory) as session:
        snapshot = SystemHealthRepository(session).snapshot(max_age_seconds=30)

    assert snapshot["database"] == HealthStatus.ERROR
