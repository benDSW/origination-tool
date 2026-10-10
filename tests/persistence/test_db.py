"""Engine construction: URL normalisation and SQLite safety settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool

from app.persistence import (
    SqlStore,
    create_all,
    drop_all,
    make_engine,
    make_session_factory,
    normalise_database_url,
)
from tests.persistence.factories import founder, sqlite_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        ("postgresql://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("postgresql+psycopg://u@host/db", "postgresql+psycopg://u@host/db"),
        ("sqlite:///./x.db", "sqlite:///./x.db"),
    ],
)
def test_normalise_database_url(raw: str, expected: str) -> None:
    assert normalise_database_url(raw) == expected


def test_sqlite_engine_enforces_foreign_keys(tmp_path: Path) -> None:
    engine = make_engine(sqlite_url(tmp_path / "fk.db"))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()


def test_in_memory_sqlite_shares_one_connection() -> None:
    engine = make_engine("sqlite://")
    try:
        assert isinstance(engine.pool, StaticPool)
        create_all(engine)
        store = SqlStore(make_session_factory(engine))
        saved = store.add_founder(founder(1))
        assert store.get_founder(saved.id or 0) == saved
        drop_all(engine)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM sqlite_master WHERE type='table'")).scalar() == 0
    finally:
        engine.dispose()


def test_postgres_engine_uses_psycopg_driver_without_connecting() -> None:
    engine = make_engine("postgres://user:pw@db.invalid:5432/app")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        # Never echo credentials in the engine's repr.
        assert "pw" not in repr(engine)
    finally:
        engine.dispose()
