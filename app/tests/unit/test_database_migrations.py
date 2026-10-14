from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.storage.db import Base, create_session_factory, current_schema_revision, init_db


def migration_head():
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[2] / "storage" / "migrations")
    )
    return ScriptDirectory.from_config(config).get_current_head()


def test_fresh_database_is_migrated_to_head(settings) -> None:
    session_factory = create_session_factory(settings)

    init_db(session_factory)

    assert current_schema_revision(session_factory) == migration_head()
    columns = {column["name"] for column in inspect(session_factory.kw["bind"]).get_columns("users")}
    assert {"biometric_consent", "access_revoked"} <= columns


def test_unversioned_create_all_database_is_safely_adopted(settings) -> None:
    session_factory = create_session_factory(settings)
    Base.metadata.create_all(bind=session_factory.kw["bind"])

    init_db(session_factory)

    assert current_schema_revision(session_factory) == migration_head()
