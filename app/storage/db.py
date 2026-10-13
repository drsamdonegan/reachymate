"""Database helpers."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import Settings

Base = declarative_base()


def create_session_factory(settings: Settings) -> sessionmaker[Session]:
    """Create the configured session factory."""

    connect_args = {"check_same_thread": False} if settings.is_sqlite else {}
    engine = create_engine(settings.db_url, future=True, pool_pre_ping=True, connect_args=connect_args)
    if settings.is_sqlite:

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            # Python 3.11 sqlite's legacy mode does not BEGIN for SELECT or
            # SAVEPOINT. Explicit BEGIN keeps enrollment savepoints inside the
            # caller's transaction, so rolling it back really rolls back enrollment.
            connection.isolation_level = None
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(engine, "begin")
        def begin_transaction(connection):
            mode = connection.get_execution_options().get("sqlite_begin_mode", "DEFERRED")
            if mode not in {"DEFERRED", "IMMEDIATE", "EXCLUSIVE"}:
                raise ValueError("Unsupported SQLite transaction mode")
            connection.exec_driver_sql("BEGIN " + mode)

    return sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session
    )


def init_db(session_factory: sessionmaker[Session]) -> None:
    """Bring the configured database to the checked-in Alembic schema head."""

    engine = session_factory.kw["bind"]
    _bootstrap_unversioned_database(engine)
    _run_alembic(engine, "upgrade", "head")


def current_schema_revision(session_factory: sessionmaker[Session]) -> str | None:
    """Return the durable Alembic revision for diagnostics and startup checks."""

    engine = session_factory.kw["bind"]
    with engine.connect() as connection:
        if "alembic_version" not in inspect(connection).get_table_names():
            return None
        return connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one_or_none()


def _bootstrap_unversioned_database(engine) -> None:  # type: ignore[no-untyped-def]
    """Safely adopt databases made by the former ``create_all`` startup path."""

    legacy_expected = {
        "users",
        "face_embeddings",
        "access_events",
        "system_health",
        "salto_tokens",
    }
    expected = legacy_expected | {"unlock_commands"}
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        existing = expected & tables
        if "alembic_version" in tables or not existing:
            return
        if frozenset(existing) not in {frozenset(legacy_expected), frozenset(expected)}:
            missing = ", ".join(sorted(legacy_expected - existing))
            raise RuntimeError(f"Refusing to adopt a partial unversioned database; missing: {missing}")
        user_columns = {column["name"] for column in inspector.get_columns("users")}

    controls = {"biometric_consent", "access_revoked"}
    present_controls = controls & user_columns
    if not present_controls:
        _run_alembic(engine, "stamp", "0001_initial")
        return
    if present_controls == controls:
        revision = (
            "0003_durable_unlock_commands" if "unlock_commands" in existing else "0002_member_access_safety"
        )
        # create_all databases may already contain the new schema; do not try
        # to add its columns a second time during adoption.
        if "greet_by_name" in user_columns and "biometric_consents" in tables:
            revision = "0005_consent_template_model"
            if "release_attempts" in tables:
                revision = "0006_release_attempts"
        _run_alembic(engine, "stamp", revision)
        return
    raise RuntimeError("Refusing to adopt a partially migrated users table")


def _run_alembic(engine, operation: str, revision: str) -> None:  # type: ignore[no-untyped-def]
    root = Path(__file__).resolve().parents[2]
    # Build a self-contained config so startup does not re-read .env or mutate
    # process logging through alembic.ini.
    config = Config()
    config.set_main_option("script_location", str(root / "app" / "storage" / "migrations"))
    config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    config.attributes["settings"] = SimpleNamespace(db_url=engine.url.render_as_string(hide_password=False))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        if operation == "upgrade":
            command.upgrade(config, revision)
        elif operation == "stamp":
            command.stamp(config, revision)
        else:  # pragma: no cover - internal invariant
            raise ValueError(operation)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    """Yield a transactional session."""

    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
