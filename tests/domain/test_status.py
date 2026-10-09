"""R16: outreach transitions follow the state machine and are audited."""

import pytest

from app.domain.models import CloseReason, EmailSource, EventSource, OutreachRecord, OutreachStatus
from app.domain.status import ALLOWED_TRANSITIONS, InvalidTransition, can_transition, transition
from tests.fakes import utc

S = OutreachStatus
T0 = utc(2026, 10, 1)
T1 = utc(2026, 10, 2)


def make(status: OutreachStatus = S.DISCOVERED, **kw: object) -> OutreachRecord:
    return OutreachRecord(id=1, founder_id=1, status=status, created_at=T0, updated_at=T0, **kw)


def test_every_status_has_a_transition_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_closed_is_terminal() -> None:
    assert all(not can_transition(S.CLOSED, t) for t in S)


@pytest.mark.parametrize(
    ("start", "target", "stamp"),
    [
        (S.DRAFTED, S.SENT, "sent_at"),
        (S.SENT, S.REPLIED, "replied_at"),
        (S.REPLIED, S.DECK_RECEIVED, "deck_received_at"),
        (S.DISCOVERED, S.DRAFTED, "drafted_at"),
        (S.SENT, S.CLOSED, "closed_at"),
    ],
)
def test_transition_stamps_timestamp_and_returns_event(start: S, target: S, stamp: str) -> None:
    o = make(start)
    event = transition(o, target, source=EventSource.MANUAL, at=T1, note="n")
    assert o.status is target
    assert getattr(o, stamp) == T1
    assert o.updated_at == T1
    assert (event.from_status, event.to_status, event.source, event.note) == (
        start,
        target,
        EventSource.MANUAL,
        "n",
    )


@pytest.mark.parametrize(
    ("start", "target"),
    [(S.DISCOVERED, S.SENT), (S.CLOSED, S.DRAFTED), (S.DECK_RECEIVED, S.SENT), (S.NEEDS_EMAIL, S.SENT)],
)
def test_invalid_transitions_raise(start: S, target: S) -> None:
    o = make(start)
    with pytest.raises(InvalidTransition):
        transition(o, target, source=EventSource.MANUAL, at=T1)
    assert o.status is start


def test_unsaved_outreach_cannot_transition() -> None:
    o = OutreachRecord(founder_id=1, created_at=T0, updated_at=T0)
    with pytest.raises(ValueError, match="persisted"):
        transition(o, S.DRAFTED, source=EventSource.PIPELINE, at=T1)


def test_existing_sent_at_is_not_overwritten_by_later_detection() -> None:
    o = make(S.DRAFTED, sent_at=T0)
    transition(o, S.SENT, source=EventSource.AUTO_INBOX, at=T1)
    assert o.sent_at == T0


def test_override_exclusion_clears_reason() -> None:
    from app.domain.models import ExclusionReason

    o = make(S.EXCLUDED, exclusion_reason=ExclusionReason.IN_AFFINITY, exclusion_detail="x")
    transition(o, S.DRAFTED, source=EventSource.MANUAL, at=T1)
    assert o.exclusion_reason is None and o.exclusion_detail is None


def test_back_to_needs_email_discards_draft() -> None:
    o = make(
        S.DRAFTED, email="a@b.co", email_source=EmailSource.ROCKETREACH, draft_subject="s", draft_body="b"
    )
    transition(o, S.NEEDS_EMAIL, source=EventSource.MANUAL, at=T1)
    assert o.email is None and o.draft_body is None and o.drafted_at is None


def test_close_reason_defaults_to_skipped() -> None:
    o = make(S.DRAFTED)
    transition(o, S.CLOSED, source=EventSource.MANUAL, at=T1)
    assert o.close_reason is CloseReason.SKIPPED
    o2 = make(S.SENT)
    transition(o2, S.CLOSED, source=EventSource.MANUAL, at=T1, close_reason=CloseReason.DECLINED)
    assert o2.close_reason is CloseReason.DECLINED
