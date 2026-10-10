"""SqlStore: email-lookup cache, run lock, run history, template versions (R3, R4, R10, R12, R14)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from app.domain.models import (
    EmailLookupRecord,
    EmailSource,
    EmailTemplate,
    LookupOutcome,
    RunCounts,
    RunRecord,
    RunState,
    RunTrigger,
)
from app.domain.ports import NotFoundError
from app.domain.templating import DEFAULT_TEMPLATE
from app.persistence import SqlStore
from tests.persistence.factories import T0, founder

# --------------------------------------------------------------------------- email lookups


def _lookup(fid: int, **kw: object) -> EmailLookupRecord:
    base: dict[str, object] = {
        "founder_id": fid,
        "provider": EmailSource.ROCKETREACH,
        "outcome": LookupOutcome.ERROR,
        "reason": "timeout",
        "created_at": T0,
    }
    base.update(kw)
    return EmailLookupRecord(**base)  # type: ignore[arg-type]


def test_email_lookup_round_trip_and_upsert(store: SqlStore) -> None:
    fid = store.add_founder(founder(1)).id or 0
    assert store.get_email_lookup(fid, EmailSource.ROCKETREACH) is None

    first = _lookup(fid)
    store.add_email_lookup(first)
    assert store.get_email_lookup(fid, EmailSource.ROCKETREACH) == first

    retry = _lookup(
        fid,
        outcome=LookupOutcome.FOUND,
        email="alex1@example1.test",
        grade="A",
        reason=None,
        created_at=T0 + timedelta(days=1),
    )
    store.add_email_lookup(retry)
    loaded = store.get_email_lookup(fid, EmailSource.ROCKETREACH)
    assert loaded == retry
    assert loaded is not None
    assert loaded.outcome is LookupOutcome.FOUND
    assert loaded.created_at.tzinfo is not None
    # Still exactly one cache row for (founder, provider).
    assert store.count_email_lookups_since(EmailSource.ROCKETREACH, T0 - timedelta(days=10)) == 1
    # A different provider is a separate row.
    store.add_email_lookup(_lookup(fid, provider=EmailSource.EVERTRACE, outcome=LookupOutcome.NOT_FOUND))
    assert store.get_email_lookup(fid, EmailSource.EVERTRACE) is not None


def test_add_email_lookup_for_missing_founder_raises(store: SqlStore) -> None:
    with pytest.raises(NotFoundError):
        store.add_email_lookup(_lookup(999))


def test_count_email_lookups_since(store: SqlStore) -> None:
    for n, hours in ((1, 0), (2, 5), (3, 30)):
        fid = store.add_founder(founder(n)).id or 0
        store.add_email_lookup(_lookup(fid, created_at=T0 + timedelta(hours=hours)))
    fid4 = store.add_founder(founder(4)).id or 0
    store.add_email_lookup(_lookup(fid4, provider=EmailSource.EVERTRACE, created_at=T0 + timedelta(hours=40)))

    rr = EmailSource.ROCKETREACH
    assert store.count_email_lookups_since(rr, T0) == 3
    assert store.count_email_lookups_since(rr, T0 + timedelta(hours=5)) == 2  # inclusive
    assert store.count_email_lookups_since(rr, T0 + timedelta(hours=24)) == 1
    assert store.count_email_lookups_since(rr, T0 + timedelta(days=5)) == 0
    assert store.count_email_lookups_since(EmailSource.EVERTRACE, T0) == 1


# --------------------------------------------------------------------------- locks


def test_lock_acquire_contend_release(store: SqlStore) -> None:
    assert store.acquire_owned_lock("daily", owner="other-holder", now=T0, ttl_seconds=3600) is True
    assert (
        store.acquire_owned_lock(
            "daily", owner="other-holder", now=T0 + timedelta(minutes=5), ttl_seconds=3600
        )
        is False
    )
    # Different lock names are independent.
    assert store.acquire_owned_lock("tracking", owner="other-holder", now=T0, ttl_seconds=3600) is True
    store.release_owned_lock("daily", owner="other-holder")
    assert (
        store.acquire_owned_lock(
            "daily", owner="other-holder", now=T0 + timedelta(minutes=6), ttl_seconds=3600
        )
        is True
    )


def test_expired_lock_can_be_stolen_once(store: SqlStore) -> None:
    assert store.acquire_owned_lock("daily", owner="other-holder", now=T0, ttl_seconds=60)
    exactly_ttl = T0 + timedelta(seconds=60)
    assert store.acquire_owned_lock("daily", owner="other-holder", now=exactly_ttl, ttl_seconds=60) is False
    later = T0 + timedelta(seconds=61)
    assert store.acquire_owned_lock("daily", owner="other-holder", now=later, ttl_seconds=60) is True
    # The steal refreshed acquired_at, so a second contender at the same instant loses.
    assert store.acquire_owned_lock("daily", owner="other-holder", now=later, ttl_seconds=60) is False


def test_release_unheld_lock_is_noop(store: SqlStore) -> None:
    store.release_owned_lock("never-taken", owner="other-holder")
    assert store.acquire_owned_lock("never-taken", owner="other-holder", now=T0, ttl_seconds=10)


# --------------------------------------------------------------------------- runs


def test_run_round_trip_and_update(store: SqlStore) -> None:
    run = store.add_run(RunRecord(trigger=RunTrigger.CRON, started_at=T0, dry_run=True))
    assert run.id is not None
    assert run.state is RunState.RUNNING
    assert run.counts == RunCounts()
    assert run.errors == []

    run.state = RunState.PARTIAL
    run.finished_at = T0 + timedelta(minutes=3)
    run.counts.searches = 2
    run.counts.drafted = 5
    run.counts.errors = 1
    run.errors.append("search sch_test1: evertrace unavailable")
    store.save_run(run)

    latest = store.latest_run()
    assert latest == run
    assert latest is not None
    assert latest.trigger is RunTrigger.CRON
    assert latest.dry_run is True
    assert latest.counts.drafted == 5
    assert latest.finished_at is not None and latest.finished_at.tzinfo is not None


def test_save_run_validation(store: SqlStore) -> None:
    with pytest.raises(ValueError, match="persisted"):
        store.save_run(RunRecord(trigger=RunTrigger.MANUAL, started_at=T0))
    with pytest.raises(NotFoundError):
        store.save_run(RunRecord(id=77, trigger=RunTrigger.MANUAL, started_at=T0))


def test_list_runs_newest_first_with_limit(store: SqlStore) -> None:
    assert store.latest_run() is None
    assert store.list_runs() == []
    ids = [
        store.add_run(RunRecord(trigger=RunTrigger.MANUAL, started_at=T0 + timedelta(hours=h))).id
        for h in (2, 0, 1)
    ]
    assert [r.id for r in store.list_runs()] == [ids[0], ids[2], ids[1]]
    assert [r.id for r in store.list_runs(limit=1)] == [ids[0]]
    latest = store.latest_run()
    assert latest is not None and latest.id == ids[0]


def test_run_counts_tolerate_unknown_keys(store: SqlStore, engine: Engine) -> None:
    run = store.add_run(RunRecord(trigger=RunTrigger.CRON, started_at=T0))
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET counts = :c WHERE id = :id"),
            {"c": '{"drafted": 3, "retired_metric": 9}', "id": run.id},
        )
    latest = store.latest_run()
    assert latest is not None and latest.counts.drafted == 3


# --------------------------------------------------------------------------- template


def test_get_template_seeds_default_once(store: SqlStore) -> None:
    first = store.get_template()
    assert first == DEFAULT_TEMPLATE
    assert store.get_template() == DEFAULT_TEMPLATE
    assert store.list_template_versions() == [DEFAULT_TEMPLATE]


def test_save_template_creates_new_version_and_keeps_history(store: SqlStore) -> None:
    store.get_template()
    v2 = store.save_template("Quick question", "Hi {first_name},\nAre you raising?")
    assert v2 == EmailTemplate(subject="Quick question", body="Hi {first_name},\nAre you raising?", version=2)
    v3 = store.save_template("Hello", "Body {company_name}")
    assert v3.version == 3
    assert store.get_template() == v3
    assert [t.version for t in store.list_template_versions()] == [3, 2, 1]


def test_save_template_before_any_read_keeps_default_as_v1(store: SqlStore) -> None:
    saved = store.save_template("Subject", "Body")
    assert saved.version == 2
    versions = store.list_template_versions()
    assert versions[-1] == DEFAULT_TEMPLATE
