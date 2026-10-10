"""SqlStore: outreach records, audit events, dashboard read models (R2, R7, R16, R17)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.models import (
    CloseReason,
    EmailSource,
    EventSource,
    ExclusionReason,
    OutreachStatus,
    StatusEvent,
)
from app.domain.ports import NotFoundError
from app.domain.status import transition
from app.persistence import ConflictError, SqlStore, repositories
from tests.persistence.factories import T0, add_search, founder, outreach

S = OutreachStatus


def _new(store: SqlStore, n: int, **kw: object) -> tuple[int, int]:
    f = store.add_founder(founder(n))
    o = store.add_outreach(outreach(f.id or 0, **kw))
    return f.id or 0, o.id or 0


def test_add_and_get_outreach_round_trip_with_enums(store: SqlStore) -> None:
    f = store.add_founder(founder(1))
    original = outreach(
        f.id or 0,
        status=S.EXCLUDED,
        exclusion_reason=ExclusionReason.IN_AFFINITY,
        exclusion_detail="Origination Opportunities: 'Example1' (matched on company name)",
    )
    saved = store.add_outreach(original)
    assert saved.id is not None
    loaded = store.get_outreach(saved.id)
    assert loaded == saved
    assert loaded.status is S.EXCLUDED
    assert loaded.exclusion_reason is ExclusionReason.IN_AFFINITY
    assert loaded.created_at.tzinfo is not None
    assert store.get_outreach_for_founder(f.id or 0) == loaded


def test_get_outreach_missing(store: SqlStore) -> None:
    with pytest.raises(NotFoundError):
        store.get_outreach(1)
    assert store.get_outreach_for_founder(1) is None


def test_one_outreach_per_founder(store: SqlStore) -> None:
    fid, _ = _new(store, 1)
    with pytest.raises(ConflictError):
        store.add_outreach(outreach(fid))


def test_add_outreach_for_missing_founder_raises(store: SqlStore) -> None:
    with pytest.raises(NotFoundError):
        store.add_outreach(outreach(999))


def test_save_outreach_persists_record_and_event_atomically(store: SqlStore) -> None:
    _, oid = _new(store, 1)
    o = store.get_outreach(oid)
    o.email = "alex1@example1.test"
    o.email_source = EmailSource.EVERTRACE
    o.draft_subject = "Funding Enquiry"
    o.draft_body = "Hi Alex1,\n\nBody"
    o.template_version = 1
    t1 = T0 + timedelta(hours=1)
    ev1 = transition(o, S.DRAFTED, source=EventSource.PIPELINE, at=t1)
    store.save_outreach(o, ev1)
    t2 = T0 + timedelta(hours=2)
    ev2 = transition(o, S.SENT, source=EventSource.AUTO_INBOX, at=t2, note="detected")
    saved = store.save_outreach(o, ev2)

    loaded = store.get_outreach(oid)
    assert loaded == saved
    assert loaded.status is S.SENT
    assert loaded.email_source is EmailSource.EVERTRACE
    assert loaded.drafted_at == t1
    assert loaded.sent_at == t2
    assert loaded.draft_body == "Hi Alex1,\n\nBody"

    events = store.list_status_events(oid)
    assert events == [ev1, ev2]
    assert events[1].source is EventSource.AUTO_INBOX
    assert events[1].at.tzinfo is not None


def test_save_outreach_without_event(store: SqlStore) -> None:
    _, oid = _new(store, 1)
    o = store.get_outreach(oid)
    o.last_tracked_at = T0 + timedelta(days=1)
    store.save_outreach(o)
    assert store.get_outreach(oid).last_tracked_at == T0 + timedelta(days=1)
    assert store.list_status_events(oid) == []


def test_save_outreach_rolls_back_record_when_event_fails(
    store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomicity: if writing the audit event fails, the record change is not kept either."""
    _, oid = _new(store, 1)
    o = store.get_outreach(oid)
    event = transition(o, S.CLOSED, source=EventSource.MANUAL, at=T0 + timedelta(hours=1))

    def boom(_event: StatusEvent) -> object:
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(repositories, "_event_row", boom)
    with pytest.raises(RuntimeError):
        store.save_outreach(o, event)

    assert store.get_outreach(oid).status is S.DISCOVERED
    assert store.list_status_events(oid) == []


def test_save_outreach_validation(store: SqlStore) -> None:
    _, oid = _new(store, 1)
    o = store.get_outreach(oid)
    with pytest.raises(ValueError, match="persisted"):
        store.save_outreach(outreach(o.founder_id))
    wrong = StatusEvent(
        outreach_id=oid + 1, from_status=S.DISCOVERED, to_status=S.CLOSED, source=EventSource.MANUAL, at=T0
    )
    with pytest.raises(ValueError, match="different outreach"):
        store.save_outreach(o, wrong)
    o.id = 999
    with pytest.raises(NotFoundError):
        store.save_outreach(o)


def test_save_outreach_cannot_move_onto_another_founder(store: SqlStore) -> None:
    f1, _ = _new(store, 1)
    _, o2 = _new(store, 2)
    o = store.get_outreach(o2)
    o.founder_id = f1
    with pytest.raises(ConflictError):
        store.save_outreach(o)


def test_list_outreach_newest_first_with_filter_and_limit(store: SqlStore) -> None:
    ids = []
    for n, status in enumerate([S.DRAFTED, S.EXCLUDED, S.DRAFTED, S.SENT], start=1):
        _, oid = _new(store, n, status=status, updated_at=T0 + timedelta(hours=n))
        ids.append(oid)

    all_views = store.list_outreach()
    assert [v.outreach.id for v in all_views] == list(reversed(ids))

    drafted = store.list_outreach(frozenset({S.DRAFTED}))
    assert [v.outreach.id for v in drafted] == [ids[2], ids[0]]
    assert all(v.outreach.status is S.DRAFTED for v in drafted)

    pipeline = store.list_outreach(frozenset({S.DRAFTED, S.SENT}), limit=2)
    assert [v.outreach.id for v in pipeline] == [ids[3], ids[2]]

    assert store.list_outreach(frozenset()) == []
    assert store.list_outreach(limit=0) == []


def test_list_outreach_view_includes_founder_and_search_titles(store: SqlStore) -> None:
    fid, oid = _new(store, 1)
    _new(store, 2)
    s_b = add_search(store, 1, "Beta search")
    s_a = add_search(store, 2, "Alpha search")
    store.link_founder_to_search(fid, s_b.id or 0)
    store.link_founder_to_search(fid, s_a.id or 0)

    views = {v.outreach.id: v for v in store.list_outreach()}
    view = views[oid]
    assert view.founder.id == fid
    assert view.founder.first_name == "Alex1"
    assert view.search_titles == ("Alpha search", "Beta search")
    other = next(v for k, v in views.items() if k != oid)
    assert other.search_titles == ()

    assert store.get_outreach_view(oid) == view


def test_get_outreach_view_missing(store: SqlStore) -> None:
    with pytest.raises(NotFoundError):
        store.get_outreach_view(7)


def test_count_by_status_reports_every_status(store: SqlStore) -> None:
    assert store.count_by_status() == dict.fromkeys(S, 0)
    _new(store, 1, status=S.DRAFTED)
    _new(store, 2, status=S.DRAFTED)
    _new(store, 3, status=S.CLOSED, close_reason=CloseReason.DECLINED)
    counts = store.count_by_status()
    assert counts[S.DRAFTED] == 2
    assert counts[S.CLOSED] == 1
    assert counts[S.SENT] == 0
    assert set(counts) == set(S)


def test_status_events_oldest_first(store: SqlStore) -> None:
    _, oid = _new(store, 1)
    o = store.get_outreach(oid)
    later = transition(o, S.NEEDS_EMAIL, source=EventSource.PIPELINE, at=T0 + timedelta(hours=5))
    store.save_outreach(o, later)
    # An event recorded afterwards but stamped earlier still sorts by time.
    earlier = StatusEvent(
        outreach_id=oid,
        from_status=S.DISCOVERED,
        to_status=S.DISCOVERED,
        source=EventSource.MANUAL,
        at=T0,
        note="n",
    )
    store.save_outreach(o, earlier)
    assert store.list_status_events(oid) == [earlier, later]
