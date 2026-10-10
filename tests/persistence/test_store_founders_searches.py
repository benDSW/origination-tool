"""SqlStore: searches, founders, founder<->search links, erasure (R2, R18, privacy)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, func, select, text

from app.domain.models import (
    EmailLookupRecord,
    EmailSource,
    EventSource,
    LocationPolicy,
    LookupOutcome,
    OutreachStatus,
    SavedSearch,
    SearchConfig,
)
from app.domain.ports import NotFoundError, Store
from app.domain.status import transition
from app.persistence import ConflictError, SqlStore
from app.persistence.models import EmailLookupRow, FounderSearchRow, OutreachRow, StatusEventRow
from tests.persistence.factories import T0, add_search, founder, outreach


def test_sqlstore_satisfies_store_port(store: SqlStore) -> None:
    assert isinstance(store, Store)


# --------------------------------------------------------------------------- searches


def test_upsert_inserts_new_search_disabled_with_defaults(store: SqlStore) -> None:
    cfg = add_search(store, 1, "Deep tech")
    assert cfg.id is not None
    assert cfg.external_id == "sch_test1"
    assert cfg.title == "Deep tech"
    assert cfg.enabled is False
    assert cfg.location_policy is LocationPolicy.UK_OUTSIDE_LONDON
    assert cfg.lookback_days == 30
    assert cfg.watermark is None
    assert cfg.last_synced_at == T0
    assert cfg.last_synced_at.tzinfo is not None


def test_upsert_refreshes_title_but_never_local_settings(store: SqlStore) -> None:
    cfg = add_search(store, 1, "Old title")
    cfg.enabled = True
    cfg.location_policy = LocationPolicy.LONDON_ALLOWED
    cfg.lookback_days = 90
    cfg.watermark = T0 + timedelta(hours=3)
    store.save_search_config(cfg)

    later = T0 + timedelta(days=1)
    again = store.upsert_search_from_catalog(
        SavedSearch(external_id="sch_test1", title="New title"), synced_at=later
    )

    assert again.id == cfg.id
    assert again.title == "New title"
    assert again.last_synced_at == later
    assert again.enabled is True
    assert again.location_policy is LocationPolicy.LONDON_ALLOWED
    assert again.lookback_days == 90
    assert again.watermark == T0 + timedelta(hours=3)
    assert len(store.list_search_configs()) == 1


def test_list_and_get_search_configs(store: SqlStore) -> None:
    b = add_search(store, 2, "Beta")
    a = add_search(store, 1, "Alpha")
    assert [c.title for c in store.list_search_configs()] == ["Alpha", "Beta"]
    assert store.get_search_config(b.id or 0) == b
    assert store.get_search_config(a.id or 0).external_id == "sch_test1"


def test_get_search_config_missing_raises(store: SqlStore) -> None:
    with pytest.raises(NotFoundError):
        store.get_search_config(999)


def test_save_search_config_inserts_when_no_id(store: SqlStore) -> None:
    saved = store.save_search_config(SearchConfig(external_id="sch_local", title="Local", enabled=True))
    assert saved.id is not None
    assert store.get_search_config(saved.id).enabled is True


def test_save_search_config_missing_id_raises(store: SqlStore) -> None:
    with pytest.raises(NotFoundError):
        store.save_search_config(SearchConfig(id=42, external_id="sch_x", title="X"))


def test_save_search_config_duplicate_external_id_conflicts(store: SqlStore) -> None:
    add_search(store, 1)
    with pytest.raises(ConflictError):
        store.save_search_config(SearchConfig(external_id="sch_test1", title="dupe"))


# --------------------------------------------------------------------------- founders


def test_add_and_get_founder_round_trip(store: SqlStore) -> None:
    original = founder(1, company_registry_url="https://registry.example.test/company/00000001")
    saved = store.add_founder(original)
    assert saved.id is not None
    loaded = store.get_founder(saved.id)
    assert loaded == saved
    assert loaded.model_dump(exclude={"id"}) == original.model_dump(exclude={"id"})
    assert loaded.discovered_at.tzinfo is not None
    assert loaded.discovered_at == T0


def test_get_founder_missing_raises(store: SqlStore) -> None:
    with pytest.raises(NotFoundError):
        store.get_founder(123)


def test_find_founder_by_signal_id_or_slug(store: SqlStore) -> None:
    saved = store.add_founder(founder(1))
    assert store.find_founder(evertrace_signal_id="sig_test0001", linkedin_slug=None) == saved
    # Same person surfacing under a different signal id in another search.
    assert store.find_founder(evertrace_signal_id="sig_other", linkedin_slug="alex-testfounder-1") == saved
    assert store.find_founder(evertrace_signal_id="sig_other", linkedin_slug=None) is None
    assert store.find_founder(evertrace_signal_id="sig_other", linkedin_slug="someone-else") is None


def test_find_founder_prefers_exact_signal_id_match(store: SqlStore) -> None:
    by_slug = store.add_founder(founder(1))
    by_id = store.add_founder(founder(2))
    found = store.find_founder(evertrace_signal_id="sig_test0002", linkedin_slug=by_slug.linkedin_slug)
    assert found == by_id


def test_founder_dedupe_constraints(store: SqlStore) -> None:
    store.add_founder(founder(1))
    with pytest.raises(ConflictError):
        store.add_founder(founder(1, linkedin_slug="different-slug"))
    with pytest.raises(ConflictError):
        store.add_founder(founder(2, linkedin_slug="alex-testfounder-1"))


def test_many_founders_without_linkedin_slug_are_allowed(store: SqlStore) -> None:
    store.add_founder(founder(1, linkedin_slug=None))
    store.add_founder(founder(2, linkedin_slug=None))
    assert store.find_founder(evertrace_signal_id="sig_test0002", linkedin_slug=None) is not None


def test_conflict_error_does_not_leak_personal_data(store: SqlStore) -> None:
    store.add_founder(founder(1))
    with pytest.raises(ConflictError) as info:
        store.add_founder(founder(1))
    assert "Alex1" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_naive_datetimes_are_rejected(store: SqlStore) -> None:
    naive = T0.replace(tzinfo=None)
    with pytest.raises(ValueError, match="naive") as info:
        store.add_founder(founder(1, created_at=naive))
    # The SQL statement and its parameters (founder PII) are not in the error.
    assert "Alex1" not in str(info.value)
    assert "INSERT" not in str(info.value)
    assert info.value.__cause__ is None


def test_non_utc_datetimes_are_stored_as_utc(store: SqlStore) -> None:
    from datetime import timezone

    plus_two = T0.astimezone(timezone(timedelta(hours=2)))
    saved = store.add_founder(founder(1, discovered_at=plus_two))
    loaded = store.get_founder(saved.id or 0)
    assert loaded.discovered_at == T0
    assert loaded.discovered_at.utcoffset() == timedelta(0)


# --------------------------------------------------------------------------- links


def test_link_founder_to_search_is_idempotent(store: SqlStore, engine: Engine) -> None:
    f = store.add_founder(founder(1))
    s = add_search(store, 1)
    store.link_founder_to_search(f.id or 0, s.id or 0)
    store.link_founder_to_search(f.id or 0, s.id or 0)
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(FounderSearchRow)) == 1


def test_link_founder_to_search_missing_ids_raise(store: SqlStore) -> None:
    f = store.add_founder(founder(1))
    s = add_search(store, 1)
    with pytest.raises(NotFoundError):
        store.link_founder_to_search(999, s.id or 0)
    with pytest.raises(NotFoundError):
        store.link_founder_to_search(f.id or 0, 999)


# --------------------------------------------------------------------------- erasure


def _populate_founder(store: SqlStore, n: int) -> tuple[int, int]:
    f = store.add_founder(founder(n))
    fid = f.id or 0
    s = add_search(store, n)
    store.link_founder_to_search(fid, s.id or 0)
    o = store.add_outreach(outreach(fid))
    event = transition(o, OutreachStatus.NEEDS_EMAIL, source=EventSource.PIPELINE, at=T0)
    store.save_outreach(o, event)
    store.add_email_lookup(
        EmailLookupRecord(
            founder_id=fid, provider=EmailSource.ROCKETREACH, outcome=LookupOutcome.NOT_FOUND, created_at=T0
        )
    )
    return fid, o.id or 0


def test_delete_founder_erases_everything(store: SqlStore, engine: Engine) -> None:
    fid, oid = _populate_founder(store, 1)
    keep_fid, keep_oid = _populate_founder(store, 2)

    store.delete_founder(fid)

    with pytest.raises(NotFoundError):
        store.get_founder(fid)
    with pytest.raises(NotFoundError):
        store.get_outreach(oid)
    assert store.get_outreach_for_founder(fid) is None
    assert store.list_status_events(oid) == []
    assert store.get_email_lookup(fid, EmailSource.ROCKETREACH) is None
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(FounderSearchRow)) == 1
    # The other founder is untouched, and searches survive.
    assert store.get_founder(keep_fid).id == keep_fid
    assert len(store.list_status_events(keep_oid)) == 1
    assert len(store.list_search_configs()) == 2


def test_delete_founder_missing_raises(store: SqlStore) -> None:
    with pytest.raises(NotFoundError):
        store.delete_founder(404)


def test_database_level_cascade_from_founders(store: SqlStore, engine: Engine) -> None:
    """ON DELETE CASCADE is real (FK enforcement is on for SQLite connections)."""
    fid, _ = _populate_founder(store, 1)
    with engine.begin() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        conn.execute(text("DELETE FROM founders WHERE id = :id"), {"id": fid})
    with engine.connect() as conn:
        for model in (OutreachRow, StatusEventRow, EmailLookupRow, FounderSearchRow):
            assert conn.scalar(select(func.count()).select_from(model)) == 0, model.__tablename__
