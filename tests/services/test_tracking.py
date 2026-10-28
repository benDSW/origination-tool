"""TrackingService (R15): sent / replied detected through the InboxTracker port."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.models import (
    EventSource,
    InboxEvent,
    InboxEventKind,
    OutreachRecord,
    OutreachStatus,
)
from app.domain.ports import IntegrationError
from app.persistence import SqlStore
from app.services.origination import _founder_from_signal
from app.services.tracking import TrackingService
from tests.fakes import FakeInbox, make_signal
from tests.services.conftest import NOW, FakeClock

S = OutreachStatus
DRAFTED_AT = NOW - timedelta(days=3)


def _outreach(store: SqlStore, n: int, status: OutreachStatus, email: str | None) -> OutreachRecord:
    founder = store.add_founder(_founder_from_signal(make_signal(n), NOW - timedelta(days=5)))
    assert founder.id is not None
    record = OutreachRecord(
        founder_id=founder.id,
        status=status,
        email=email,
        created_at=NOW - timedelta(days=5),
        updated_at=DRAFTED_AT,
        drafted_at=DRAFTED_AT if status in (S.DRAFTED, S.SENT) else None,
        sent_at=DRAFTED_AT + timedelta(hours=1) if status is S.SENT else None,
    )
    return store.add_outreach(record)


def _event(kind: InboxEventKind, hours_after_draft: float) -> InboxEvent:
    return InboxEvent(kind=kind, occurred_at=DRAFTED_AT + timedelta(hours=hours_after_draft))


def test_sent_detected_moves_drafted_to_sent(store: SqlStore, clock: FakeClock) -> None:
    o = _outreach(store, 1, S.DRAFTED, "a@co1.example")
    inbox = FakeInbox(events={"a@co1.example": [_event(InboxEventKind.SENT, 2)]})

    result = TrackingService(store, inbox, clock).sync()

    assert (result.updates, result.checked, result.errors) == (1, 1, [])
    saved = store.get_outreach(o.id or 0)
    assert saved.status is S.SENT
    assert saved.sent_at == DRAFTED_AT + timedelta(hours=2)
    assert saved.last_tracked_at == NOW
    events = store.list_status_events(o.id or 0)
    assert [(e.from_status, e.to_status, e.source) for e in events] == [
        (S.DRAFTED, S.SENT, EventSource.AUTO_INBOX)
    ]
    assert events[0].at == DRAFTED_AT + timedelta(hours=2)


def test_sent_then_replied_applied_in_order(store: SqlStore, clock: FakeClock) -> None:
    o = _outreach(store, 1, S.DRAFTED, "a@co1.example")
    inbox = FakeInbox(
        events={"a@co1.example": [_event(InboxEventKind.REPLIED, 30), _event(InboxEventKind.SENT, 2)]}
    )

    result = TrackingService(store, inbox, clock).sync()

    assert result.updates == 2
    saved = store.get_outreach(o.id or 0)
    assert saved.status is S.REPLIED
    assert saved.replied_at == DRAFTED_AT + timedelta(hours=30)
    assert [e.to_status for e in store.list_status_events(o.id or 0)] == [S.SENT, S.REPLIED]


def test_reply_without_detected_send_moves_drafted_to_replied(store: SqlStore, clock: FakeClock) -> None:
    o = _outreach(store, 1, S.DRAFTED, "a@co1.example")
    inbox = FakeInbox(events={"a@co1.example": [_event(InboxEventKind.REPLIED, 5)]})

    TrackingService(store, inbox, clock).sync()

    assert store.get_outreach(o.id or 0).status is S.REPLIED


def test_manual_sent_then_sent_event_is_ignored(store: SqlStore, clock: FakeClock) -> None:
    o = _outreach(store, 1, S.SENT, "a@co1.example")
    inbox = FakeInbox(
        events={"a@co1.example": [_event(InboxEventKind.SENT, 2), _event(InboxEventKind.REPLIED, 9)]}
    )

    result = TrackingService(store, inbox, clock).sync()

    assert result.updates == 1
    saved = store.get_outreach(o.id or 0)
    assert saved.status is S.REPLIED
    assert saved.sent_at == DRAFTED_AT + timedelta(hours=1)  # manual timestamp kept


def test_deck_received_event_is_honoured_from_sent(store: SqlStore, clock: FakeClock) -> None:
    o = _outreach(store, 1, S.SENT, "a@co1.example")
    inbox = FakeInbox(events={"a@co1.example": [_event(InboxEventKind.DECK_RECEIVED, 40)]})

    TrackingService(store, inbox, clock).sync()

    assert store.get_outreach(o.id or 0).status is S.DECK_RECEIVED


def test_only_trackable_outreach_with_email_is_checked(store: SqlStore, clock: FakeClock) -> None:
    _outreach(store, 1, S.DRAFTED, None)
    _outreach(store, 2, S.NEEDS_EMAIL, None)
    _outreach(store, 3, S.REPLIED, "c@co3.example")
    _outreach(store, 4, S.SENT, "d@co4.example")
    inbox = FakeInbox()

    result = TrackingService(store, inbox, clock).sync()

    assert inbox.calls == ["d@co4.example"]
    assert result.checked == 1
    assert result.updates == 0


def test_no_events_still_stamps_last_tracked(store: SqlStore, clock: FakeClock) -> None:
    o = _outreach(store, 1, S.DRAFTED, "a@co1.example")
    TrackingService(store, FakeInbox(), clock).sync()
    saved = store.get_outreach(o.id or 0)
    assert saved.status is S.DRAFTED
    assert saved.last_tracked_at == NOW


class _PartlyBrokenInbox:
    def __init__(self, broken: str, kind: type[Exception]) -> None:
        self.broken = broken
        self.kind = kind
        self.calls: list[str] = []

    def events_for(self, email: str, since: datetime) -> list[InboxEvent]:
        self.calls.append(email)
        if email == self.broken:
            if self.kind is IntegrationError:
                raise IntegrationError("affinity", "rate limited")
            raise self.kind("raw body with secrets")
        return [InboxEvent(kind=InboxEventKind.SENT, occurred_at=since + timedelta(hours=1))]


def test_one_failing_founder_does_not_stop_the_loop(store: SqlStore, clock: FakeClock) -> None:
    bad = _outreach(store, 1, S.DRAFTED, "a@co1.example")
    good = _outreach(store, 2, S.DRAFTED, "b@co2.example")
    inbox = _PartlyBrokenInbox("a@co1.example", IntegrationError)

    result = TrackingService(store, inbox, clock).sync()

    assert result.updates == 1
    assert result.errors == [f"Tracking outreach {bad.id}: [affinity] rate limited"]
    assert store.get_outreach(good.id or 0).status is S.SENT
    assert store.get_outreach(bad.id or 0).status is S.DRAFTED


def test_unexpected_errors_are_reported_by_type_only(store: SqlStore, clock: FakeClock) -> None:
    bad = _outreach(store, 1, S.DRAFTED, "a@co1.example")
    result = TrackingService(store, _PartlyBrokenInbox("a@co1.example", RuntimeError), clock).sync()
    assert result.errors == [f"Tracking outreach {bad.id}: unexpected RuntimeError"]


def test_events_before_the_draft_are_ignored(store: SqlStore, clock: FakeClock) -> None:
    o = _outreach(store, 1, S.DRAFTED, "a@co1.example")

    class _LeakyInbox:
        def events_for(self, email: str, since: datetime) -> list[InboxEvent]:
            return [InboxEvent(kind=InboxEventKind.SENT, occurred_at=since - timedelta(days=1))]

    result = TrackingService(store, _LeakyInbox(), clock).sync()

    assert result.updates == 0
    assert store.get_outreach(o.id or 0).status is S.DRAFTED


def _age(store: SqlStore, o: OutreachRecord, *, sent_days_ago: float, tracked_hours_ago: float | None):
    o.sent_at = NOW - timedelta(days=sent_days_ago)
    o.last_tracked_at = NOW - timedelta(hours=tracked_hours_ago) if tracked_hours_ago is not None else None
    return store.save_outreach(o)


def test_old_outreach_is_rechecked_at_most_daily_unless_forced(store: SqlStore, clock: FakeClock) -> None:
    recent = _age(store, _outreach(store, 1, S.SENT, "a@co1.example"), sent_days_ago=5, tracked_hours_ago=1)
    old_fresh = _age(
        store, _outreach(store, 2, S.SENT, "b@co2.example"), sent_days_ago=30, tracked_hours_ago=2
    )
    old_stale = _age(
        store, _outreach(store, 3, S.SENT, "c@co3.example"), sent_days_ago=30, tracked_hours_ago=25
    )
    old_never = _age(
        store, _outreach(store, 4, S.SENT, "d@co4.example"), sent_days_ago=30, tracked_hours_ago=None
    )
    inbox = FakeInbox()

    result = TrackingService(store, inbox, clock).sync()

    assert (result.checked, result.deferred) == (3, 1)
    assert store.get_outreach(old_fresh.id or 0).last_tracked_at == NOW - timedelta(hours=2)
    for o in (recent, old_stale, old_never):
        assert store.get_outreach(o.id or 0).last_tracked_at == NOW

    forced = TrackingService(store, inbox, clock).sync(force=True)
    assert (forced.checked, forced.deferred) == (4, 0)
