"""SqlStore support for the security-review fixes: owned run locks (finding 2), the
race-safe RocketReach budget (findings 1 and 2), erasure suppressions (finding 5)
and server-side admin sessions (finding 7). Synthetic data only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from app.domain.models import EmailLookupRecord, EmailSource, LookupOutcome
from app.domain.ports import LookupReservation, NotFoundError, Store
from app.persistence import SqlStore
from app.persistence.repositories import ALREADY_CLAIMED, CAP_REACHED, RESERVED
from tests.persistence.factories import T0, founder, outreach

DAY = T0.replace(hour=0, minute=0)
RR = EmailSource.ROCKETREACH


def test_sqlstore_satisfies_the_single_store_port(store: SqlStore) -> None:
    assert isinstance(store, Store)
    assert tuple(LookupReservation) == (RESERVED, CAP_REACHED, ALREADY_CLAIMED)


# --------------------------------------------------------------------------- owned locks


def test_lock_takeover_cannot_be_undone_by_the_previous_holder(store: SqlStore) -> None:
    """The audit's scenario: A acquires, B takes over a stale lock, A finishes and
    releases, then C tries. Before the fix C also got the lock (True/True/True)."""
    assert store.acquire_owned_lock("run", owner="a", now=T0, ttl_seconds=3600)
    assert store.acquire_owned_lock("run", owner="b", now=T0 + timedelta(seconds=3601), ttl_seconds=3600)
    store.release_owned_lock("run", owner="a")  # A no longer holds it: no effect
    assert not store.acquire_owned_lock("run", owner="c", now=T0 + timedelta(seconds=3602), ttl_seconds=3600)
    # A's heartbeat tells it the lock is gone; B's keeps it fresh.
    assert store.refresh_owned_lock("run", owner="a", now=T0 + timedelta(seconds=3603)) is False
    assert store.refresh_owned_lock("run", owner="b", now=T0 + timedelta(seconds=7000)) is True
    # Refreshed at +7000 s: not stale at +7000 + 3599 s.
    assert not store.acquire_owned_lock("run", owner="c", now=T0 + timedelta(seconds=10599), ttl_seconds=3600)
    store.release_owned_lock("run", owner="b")
    assert store.acquire_owned_lock("run", owner="c", now=T0 + timedelta(seconds=10600), ttl_seconds=3600)


# --------------------------------------------------------------------------- lookup budget


def _reservation(fid: int, **kw: object) -> EmailLookupRecord:
    base: dict[str, object] = {
        "founder_id": fid,
        "provider": RR,
        "outcome": LookupOutcome.REJECTED,
        "reason": "pending",
        "created_at": T0,
    }
    base.update(kw)
    return EmailLookupRecord(**base)  # type: ignore[arg-type]


def _founders(store: SqlStore, n: int) -> list[int]:
    return [store.add_founder(founder(i)).id or 0 for i in range(1, n + 1)]


def test_reservation_claims_the_founder_once(store: SqlStore) -> None:
    (fid,) = _founders(store, 1)
    assert store.reserve_email_lookup(_reservation(fid), day_start=DAY, daily_cap=5) == RESERVED
    cached = store.get_email_lookup(fid, RR)
    assert cached is not None and cached.outcome is LookupOutcome.REJECTED and cached.reason == "pending"
    # A second run (or a retry) can never claim the same founder again.
    assert store.reserve_email_lookup(_reservation(fid), day_start=DAY, daily_cap=5) == ALREADY_CLAIMED
    assert store.lookups_reserved_since(RR, DAY) == 1


def test_error_rows_can_be_reclaimed_but_settled_ones_cannot(store: SqlStore) -> None:
    error_fid, found_fid = _founders(store, 2)
    store.add_email_lookup(_reservation(error_fid, outcome=LookupOutcome.ERROR, reason="timeout"))
    store.add_email_lookup(_reservation(found_fid, outcome=LookupOutcome.FOUND, email="a@example1.test"))
    later = T0 + timedelta(days=1)
    next_day = DAY + timedelta(days=1)
    assert store.reserve_email_lookup(
        _reservation(error_fid, created_at=later), day_start=next_day, daily_cap=5
    ) == (RESERVED)
    assert store.reserve_email_lookup(
        _reservation(found_fid, created_at=later), day_start=next_day, daily_cap=5
    ) == (ALREADY_CLAIMED)


def test_cap_is_enforced_atomically_and_rolls_the_claim_back(store: SqlStore) -> None:
    fids = _founders(store, 4)
    results = [store.reserve_email_lookup(_reservation(f), day_start=DAY, daily_cap=3) for f in fids]
    assert results == [RESERVED, RESERVED, RESERVED, CAP_REACHED]
    assert store.get_email_lookup(fids[3], RR) is None  # nothing written for the refused one
    assert store.lookups_reserved_since(RR, DAY) == 3
    # A new UTC day has a new budget.
    next_day = DAY + timedelta(days=1)
    assert (
        store.reserve_email_lookup(
            _reservation(fids[3], created_at=T0 + timedelta(days=1)), day_start=next_day, daily_cap=3
        )
        == RESERVED
    )
    assert store.lookups_reserved_since(RR, next_day) == 1


def test_budget_is_seeded_from_lookups_already_cached_today(store: SqlStore) -> None:
    fids = _founders(store, 3)
    store.add_email_lookup(_reservation(fids[0], outcome=LookupOutcome.NOT_FOUND))
    store.add_email_lookup(_reservation(fids[1], outcome=LookupOutcome.NOT_FOUND))
    assert store.reserve_email_lookup(_reservation(fids[2]), day_start=DAY, daily_cap=2) == CAP_REACHED
    assert store.reserve_email_lookup(_reservation(fids[2]), day_start=DAY, daily_cap=3) == RESERVED
    assert store.lookups_reserved_since(RR, DAY) == 3


def test_reservation_for_unknown_founder_raises(store: SqlStore) -> None:
    with pytest.raises(NotFoundError):
        store.reserve_email_lookup(_reservation(999), day_start=DAY, daily_cap=3)


# --------------------------------------------------------------------------- suppressions


def test_erase_founder_deletes_everything_and_keeps_only_hashes(store: SqlStore, engine: Engine) -> None:
    saved = store.add_founder(founder(1, evertrace_email="alex1@example1.test"))
    fid = saved.id or 0
    store.add_outreach(outreach(fid))
    store.add_email_lookup(_reservation(fid, outcome=LookupOutcome.FOUND, email="alex1@example1.test"))
    hashes = ["a" * 64, "b" * 64, "a" * 64, ""]

    store.erase_founder(fid, suppress=hashes, at=T0)

    assert store.find_founder(evertrace_signal_id="sig_test0001", linkedin_slug="alex-testfounder-1") is None
    assert store.get_email_lookup(fid, RR) is None and store.list_outreach() == []
    assert store.is_suppressed(["a" * 64]) and store.is_suppressed(["x" * 64, "b" * 64])
    assert not store.is_suppressed(["c" * 64]) and not store.is_suppressed([])
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM suppressions")).fetchall()
    assert len(rows) == 2
    dumped = repr(rows)
    for pii in ("alex", "Alex", "testfounder", "example1", "sig_test"):
        assert pii not in dumped


def test_erase_is_idempotent_for_known_hashes_and_404s_for_unknown_founders(store: SqlStore) -> None:
    first, second = _founders(store, 2)
    store.erase_founder(first, suppress=["a" * 64], at=T0)
    store.erase_founder(second, suppress=["a" * 64, "b" * 64], at=T0)  # "a" already stored
    assert store.is_suppressed(["b" * 64])
    with pytest.raises(NotFoundError):
        store.erase_founder(first, suppress=["c" * 64], at=T0)
    assert not store.is_suppressed(["c" * 64])  # rolled back with the failed delete


# --------------------------------------------------------------------------- admin sessions


def test_admin_sessions_expire_and_can_be_revoked(store: SqlStore, engine: Engine) -> None:
    store.add_admin_session("s1", created_at=T0, expires_at=T0 + timedelta(hours=12))
    store.add_admin_session("s2", created_at=T0, expires_at=T0 + timedelta(hours=1))
    assert store.admin_session_active("s1", now=T0) and store.admin_session_active("s2", now=T0)
    assert not store.admin_session_active("s2", now=T0 + timedelta(hours=1))  # expired
    assert not store.admin_session_active("unknown", now=T0)

    store.delete_admin_session("s1")
    assert not store.admin_session_active("s1", now=T0)

    # A new login prunes expired rows.
    store.add_admin_session("s3", created_at=T0 + timedelta(hours=2), expires_at=T0 + timedelta(hours=14))
    with engine.connect() as conn:
        assert [r[0] for r in conn.execute(text("SELECT sid_hash FROM admin_sessions"))] == ["s3"]
    assert store.delete_all_admin_sessions() == 1
    assert not store.admin_session_active("s3", now=T0 + timedelta(hours=2))
