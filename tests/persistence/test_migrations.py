"""R24: the hand-written Alembic migration builds exactly the schema the models describe."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from alembic import command
from app.persistence import SqlStore, make_engine, make_session_factory
from app.persistence.models import Base
from tests.persistence.factories import founder, sqlite_url

ROOT = Path(__file__).resolve().parents[2]
TABLES = {
    "searches",
    "founders",
    "founder_searches",
    "outreach",
    "status_events",
    "email_lookups",
    "runs",
    "locks",
    "templates",
    "lookup_budget",
    "suppressions",
    "admin_sessions",
}


def _config(database_url: str, buffer: io.StringIO | None = None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"), output_buffer=buffer)
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logger"] = False  # keep pytest's logging setup intact
    return cfg


def test_alembic_ini_holds_no_database_url() -> None:
    content = (ROOT / "alembic.ini").read_text(encoding="utf-8")
    assert "sqlalchemy.url" not in content


def test_upgrade_head_matches_models_exactly(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "migrated.db")
    command.upgrade(_config(url), "head")

    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            assert set(inspect(conn).get_table_names()) >= TABLES
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            diff = compare_metadata(ctx, Base.metadata)
        assert diff == []

        # And the migrated schema is usable by the store.
        store = SqlStore(make_session_factory(engine))
        saved = store.add_founder(founder(1))
        assert store.get_founder(saved.id or 0) == saved
        assert store.get_template().version == 1
    finally:
        engine.dispose()


def test_compare_detects_drift(tmp_path: Path) -> None:
    """Guard against a vacuous comparison: an empty DB must differ from the models."""
    engine = make_engine(sqlite_url(tmp_path / "empty.db"))
    try:
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert {d[1].name for d in diff if d[0] == "add_table"} == TABLES
    finally:
        engine.dispose()


def test_downgrade_to_base_drops_everything(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "down.db")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            assert set(inspect(conn).get_table_names()) <= {"alembic_version"}
    finally:
        engine.dispose()


def test_offline_postgres_sql_is_portable() -> None:
    """Postgres DDL renders without native enum types or a live database."""
    buffer = io.StringIO()
    command.upgrade(_config("postgres://user@db.invalid/app", buffer), "head", sql=True)
    sql = buffer.getvalue()
    assert "CREATE TABLE outreach" in sql
    assert "ON DELETE CASCADE" in sql
    assert "CREATE TYPE" not in sql
    assert "CREATE INDEX ix_outreach_status" in sql


def test_env_falls_back_to_settings_database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    url = sqlite_url(tmp_path / "from_env.db")
    # Run from an empty directory so Settings can never pick up the developer's real .env.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "alembic"))
        cfg.attributes["configure_logger"] = False
        command.upgrade(cfg, "head")
    finally:
        get_settings.cache_clear()
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            assert set(inspect(conn).get_table_names()) >= TABLES
    finally:
        engine.dispose()


def test_upgrade_with_existing_connection(tmp_path: Path) -> None:
    engine = make_engine(sqlite_url(tmp_path / "conn.db"))
    try:
        with engine.begin() as conn:
            cfg = _config("unused://")
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert set(inspect(conn).get_table_names()) >= TABLES
    finally:
        engine.dispose()
