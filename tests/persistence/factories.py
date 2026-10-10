"""Synthetic test-data builders for persistence tests (no real people or companies)."""

from __future__ import annotations

from pathlib import Path

from app.domain.models import FounderRecord, OutreachRecord, OutreachStatus, SavedSearch, SearchConfig
from app.persistence import SqlStore
from tests.fakes import utc

T0 = utc(2026, 10, 1, 9, 0)


def sqlite_url(path: Path) -> str:
    return "sqlite:///" + path.as_posix()


def founder(n: int = 1, **overrides: object) -> FounderRecord:
    base: dict[str, object] = {
        "evertrace_signal_id": f"sig_test{n:04d}",
        "linkedin_slug": f"alex-testfounder-{n}",
        "first_name": f"Alex{n}",
        "last_name": "Testfounder",
        "city": "Manchester",
        "country": "United Kingdom",
        "gender": "Woman",
        "score": 8,
        "signal_type": "New Company",
        "summary": f"Building Example{n} Limited.",
        "discovered_at": T0,
        "company_name": f"Example{n} Limited",
        "company_domain": f"example{n}.test",
        "company_registry_url": None,
        "evertrace_email": None,
        "created_at": T0,
    }
    base.update(overrides)
    return FounderRecord(**base)  # type: ignore[arg-type]


def outreach(founder_id: int, **overrides: object) -> OutreachRecord:
    base: dict[str, object] = {
        "founder_id": founder_id,
        "status": OutreachStatus.DISCOVERED,
        "created_at": T0,
        "updated_at": T0,
    }
    base.update(overrides)
    return OutreachRecord(**base)  # type: ignore[arg-type]


def add_search(store: SqlStore, n: int = 1, title: str | None = None) -> SearchConfig:
    return store.upsert_search_from_catalog(
        SavedSearch(external_id=f"sch_test{n}", title=title or f"Search {n}"), synced_at=T0
    )
