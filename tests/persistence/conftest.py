"""Shared fixtures: a fresh temp-file SQLite database per test (synthetic data only)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.persistence import SqlStore, create_all, make_engine, make_session_factory
from tests.persistence.factories import sqlite_url


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_engine(sqlite_url(tmp_path / "test.db"))
    create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def store(engine: Engine) -> SqlStore:
    return SqlStore(make_session_factory(engine))
