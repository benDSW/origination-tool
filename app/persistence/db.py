"""Engine and session-factory construction.

The same code runs on SQLite (local dev, tests) and Postgres (Railway). The
SQLite-specific bits live here so nothing else has to care which one it is.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.persistence.models import Base


def normalise_database_url(database_url: str) -> str:
    """Map Railway/Heroku-style Postgres URLs onto the psycopg3 driver.

    Duplicated (deliberately) from ``app.config`` because persistence may only
    depend on the domain; the two must agree.
    """
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://") :]
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://") :]
    return database_url


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    # SQLite ignores FOREIGN KEY / ON DELETE CASCADE unless this is set per connection.
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def make_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create an Engine for ``database_url`` (SQLite or Postgres)."""
    url = make_url(normalise_database_url(database_url))
    # hide_parameters: DB exceptions otherwise embed bound values (names, emails)
    # in their message, which would then reach logs.
    kwargs: dict[str, Any] = {"echo": echo, "future": True, "hide_parameters": True}
    if url.get_backend_name() == "sqlite":
        # The web app hands sessions to background tasks on other threads.
        kwargs["connect_args"] = {"check_same_thread": False}
        if url.database in (None, "", ":memory:"):
            # One shared connection, otherwise every checkout sees an empty DB.
            kwargs["poolclass"] = StaticPool
    else:
        # Railway can drop idle connections; detect that before handing one out.
        kwargs["pool_pre_ping"] = True
    engine = create_engine(url, **kwargs)
    if url.get_backend_name() == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Sessions do not expire on commit: repositories map rows to DTOs inside the
    transaction anyway, and this avoids surprise lazy reloads afterwards."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine: Engine) -> None:
    """Create every table directly from the models (tests / throwaway DBs only;
    real databases are migrated with Alembic)."""
    Base.metadata.create_all(engine)


def drop_all(engine: Engine) -> None:
    Base.metadata.drop_all(engine)
