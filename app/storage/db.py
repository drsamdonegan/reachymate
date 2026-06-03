"""Database helpers."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import Settings


Base = declarative_base()


def create_session_factory(settings: Settings) -> sessionmaker[Session]:
    """Create the configured session factory."""

    connect_args = {"check_same_thread": False} if settings.is_sqlite else {}
    engine = create_engine(settings.db_url, future=True, pool_pre_ping=True, connect_args=connect_args)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)


def init_db(session_factory: sessionmaker[Session]) -> None:
    """Create tables for local development and tests."""

    engine = session_factory.kw["bind"]
    Base.metadata.create_all(bind=engine)


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
