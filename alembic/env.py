"""Alembic runtime environment.

Three deliberate departures from the generated template:

* **The URL comes from ``settings``, not from ``alembic.ini``.** One process,
  one user, one settings object — a URL duplicated in the ini file is a second
  place for the database location to drift, and ``DATABASE_URL`` in ``.env`` has
  to win for migrations exactly as it does for the app.
* **``render_as_batch=True`` everywhere.** SQLite cannot ``ALTER TABLE`` in any
  useful way, so Alembic must rebuild the table instead. Turning this on now
  means a future column drop or type change is a normal autogenerate rather
  than a hand-written copy-and-swap.
* **Foreign key enforcement is off for migration connections only** — see
  :func:`_migrations_run_without_fk_enforcement`.

The engine is ``backend.db.engine``, not a private one, so migrations inherit
the same WAL mode and busy timeout the app runs with; a migration against a
live WAL database otherwise fails with "database is locked" instead of waiting.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from sqlalchemy import event
from sqlmodel import SQLModel

from alembic import context

# Importing the models registers all 11 tables on SQLModel.metadata; nothing in
# this file references them by name, which is why the import looks unused.
from backend import models  # noqa: F401
from backend.config import settings
from backend.db import engine, init_db

config = context.config

# Alembic owns the console for the duration of a migration run: this replaces
# the structlog handlers installed when backend.db was imported above, so the
# "Running upgrade ..." lines actually reach the user.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


@event.listens_for(engine, "connect")
def _migrations_run_without_fk_enforcement(
    dbapi_connection: Any, connection_record: Any
) -> None:
    """Turn ``PRAGMA foreign_keys`` back off for the duration of a migration.

    Batch mode rebuilds a table as create-temp / copy / drop / rename. With
    foreign keys enforced, SQLite rewrites *other* tables' FK clauses to follow
    that rename, silently repointing them at the temporary table, and the drop
    of a referenced parent fails outright once child rows exist.

    This listener is registered on the engine instance from inside Alembic's
    env.py, so it runs after ``backend.db``'s class-level listener (which turns
    the pragma on) and only ever in an ``alembic`` process. Application
    connections keep enforcement on.

    Note this is a ``connect`` hook rather than a statement executed against the
    migration connection: executing anything on that connection first would
    autobegin a SQLAlchemy transaction, and Alembic responds to an already-open
    transaction by making its own per-migration transaction a no-op — the DDL
    then runs in pysqlite's autocommit while the ``alembic_version`` INSERT is
    rolled back at close, leaving a fully built schema that Alembic still
    believes is at ``base``.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=OFF")
    finally:
        cursor.close()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database."""
    # A fresh clone has no data/ directory, so sqlite:///data/app.db would fail
    # to open before the first revision ever ran.
    init_db()

    # Drop any connection opened (and pragma'd) before the listener above was
    # registered, so every migration connection is guaranteed to come from a
    # fresh DBAPI connect.
    engine.dispose()

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
