"""Engine, sessions and SQLite tuning.

SQLite is fine for one user, but only with WAL: the scheduler, the apply loop
and the API all touch the file concurrently, and the default rollback journal
turns that into "database is locked". Schema is Alembic's job, never this
module's.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, make_url
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from backend.config import settings
from backend.logging_setup import get_logger

log = get_logger(__name__)

engine = create_engine(
    settings.database_url,
    echo=False,
    # FastAPI hands a session to whichever thread serves the request.
    connect_args={"check_same_thread": False},
)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    """Apply per-connection PRAGMAs — SQLite forgets them on every connect."""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency. The route owns the commit."""
    with Session(engine) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts and CLIs: commit on success, roll back loudly."""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        log.exception("db_session_rolled_back")
        raise
    finally:
        session.close()


def sqlite_path() -> Path | None:
    """The on-disk database file, or None for non-file databases."""
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    if url.database == ":memory:":
        return None
    return Path(url.database)


def init_db() -> None:
    """Prepare the filesystem for the DB. Does NOT create tables — Alembic owns schema."""
    settings.ensure_directories()
    path = sqlite_path()
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    log.info("db_initialised", database_url=settings.database_url)


def persist_detached[T](session: Session, instance: T) -> T:
    """Flush a row, load its values, and detach it so the caller can read it.

    ``session_scope`` commits on the way out, and a commit expires every
    instance the session still tracks. A caller that returns a row from inside
    the scope and then reads any attribute of it therefore triggers a lazy load
    against a closed session and gets ``DetachedInstanceError`` — which is
    exactly what every runner did with its ``Run`` row, turning each successful
    pass into a traceback and a non-zero exit at the moment it reported success.

    The INSERT is already flushed by the time this returns, so expunging costs
    nothing that is persisted; it only stops SQLAlchemy expiring the values that
    were just read.
    """
    session.add(instance)
    session.flush()
    session.refresh(instance)
    session.expunge(instance)
    return instance
