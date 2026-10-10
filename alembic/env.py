"""Alembic environment.

URL resolution (first match wins), so credentials never live in alembic.ini:
1. ``config.attributes["connection"]``: an open Connection (programmatic use);
2. ``config.attributes["database_url"]``: a URL passed programmatically (tests);
3. ``sqlalchemy.url`` main option, if someone sets it on the command line;
4. ``app.config.get_settings().database_url`` (the DATABASE_URL env var).
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import Connection

from alembic import context
from app.persistence.db import make_engine, normalise_database_url
from app.persistence.models import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.attributes.get("database_url") or config.get_main_option("sqlalchemy.url")
    if not url:
        from app.config import get_settings

        url = get_settings().database_url
    return normalise_database_url(str(url))


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite cannot ALTER most things; batch mode rebuilds tables instead.
        render_as_batch=connection.dialect.name == "sqlite",
    )


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=url.startswith("sqlite"),
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = make_engine(_database_url())
    try:
        with engine.connect() as conn:
            _configure(conn)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
