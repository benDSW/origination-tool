"""AffinityInboxTracker: derives SENT / REPLIED from CRM email interactions (FakeCrm, synthetic data)."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.domain.models import EmailInteraction, InboxEvent, InboxEventKind, InteractionDirection
from app.domain.ports import InboxTracker, IntegrationError
from app.integrations.inbox import AffinityInboxTracker
from tests.fakes import FakeCrm, utc

FOUNDER = "founder@synthetic.example"
SINCE = utc(2026, 9, 1)
OUT = InteractionDirection.OUTBOUND
IN = InteractionDirection.INBOUND


def tracker_with(*interactions: tuple[InteractionDirection, datetime]) -> AffinityInboxTracker:
    crm = FakeCrm(
        interactions={FOUNDER: [EmailInteraction(direction=d, occurred_at=at) for d, at in interactions]}
    )
    return AffinityInboxTracker(crm)


def kinds(events: list[InboxEvent]) -> list[tuple[InboxEventKind, datetime]]:
    return [(e.kind, e.occurred_at) for e in events]


def test_satisfies_inbox_tracker_port() -> None:
    assert isinstance(AffinityInboxTracker(FakeCrm()), InboxTracker)


def test_nothing_yields_no_events() -> None:
    assert tracker_with().events_for(FOUNDER, SINCE) == []
    assert AffinityInboxTracker(FakeCrm()).events_for("unknown@synthetic.example", SINCE) == []


def test_sent_only() -> None:
    events = tracker_with((OUT, utc(2026, 9, 2, 9))).events_for(FOUNDER, SINCE)
    assert kinds(events) == [(InboxEventKind.SENT, utc(2026, 9, 2, 9))]


def test_sent_then_reply() -> None:
    events = tracker_with((OUT, utc(2026, 9, 2, 9)), (IN, utc(2026, 9, 3, 14))).events_for(FOUNDER, SINCE)
    assert kinds(events) == [
        (InboxEventKind.SENT, utc(2026, 9, 2, 9)),
        (InboxEventKind.REPLIED, utc(2026, 9, 3, 14)),
    ]


def test_reply_before_send_is_not_a_reply() -> None:
    events = tracker_with((IN, utc(2026, 9, 2)), (OUT, utc(2026, 9, 4))).events_for(FOUNDER, SINCE)
    assert kinds(events) == [(InboxEventKind.SENT, utc(2026, 9, 4))]


def test_inbound_at_same_instant_as_send_is_not_a_reply() -> None:
    at = utc(2026, 9, 4, 10)
    assert kinds(tracker_with((OUT, at), (IN, at)).events_for(FOUNDER, SINCE)) == [(InboxEventKind.SENT, at)]


def test_inbound_without_any_outbound_counts_as_reply() -> None:
    """Ben may have sent from a mailbox Affinity does not sync."""
    events = tracker_with((IN, utc(2026, 9, 5)), (IN, utc(2026, 9, 6))).events_for(FOUNDER, SINCE)
    assert kinds(events) == [(InboxEventKind.REPLIED, utc(2026, 9, 5))]


def test_multiple_messages_use_earliest_of_each_and_ignore_order() -> None:
    events = tracker_with(
        (IN, utc(2026, 9, 9)),
        (OUT, utc(2026, 9, 7)),  # follow-up
        (IN, utc(2026, 9, 1, 12)),  # before first send -> ignored
        (OUT, utc(2026, 9, 2)),  # first send
        (IN, utc(2026, 9, 5)),  # first reply after the send
    ).events_for(FOUNDER, SINCE)
    assert kinds(events) == [
        (InboxEventKind.SENT, utc(2026, 9, 2)),
        (InboxEventKind.REPLIED, utc(2026, 9, 5)),
    ]


def test_interactions_before_since_are_ignored() -> None:
    events = tracker_with((OUT, utc(2026, 8, 1)), (IN, utc(2026, 9, 2))).events_for(FOUNDER, SINCE)
    assert kinds(events) == [(InboxEventKind.REPLIED, utc(2026, 9, 2))]


def test_never_emits_deck_received() -> None:
    events = tracker_with(
        *[(d, utc(2026, 9, day)) for day, d in enumerate([OUT, IN, OUT, IN], 2)]
    ).events_for(FOUNDER, SINCE)
    assert InboxEventKind.DECK_RECEIVED not in {e.kind for e in events}


def test_defensive_since_filter_with_a_crm_that_ignores_since() -> None:
    class LooseCrm(FakeCrm):
        def email_interactions(self, email: str, since: datetime) -> list[EmailInteraction]:
            return self.interactions.get(email, [])

    crm = LooseCrm(interactions={FOUNDER: [EmailInteraction(direction=OUT, occurred_at=utc(2026, 1, 1))]})
    assert AffinityInboxTracker(crm).events_for(FOUNDER, SINCE) == []


def test_crm_errors_propagate() -> None:
    class BrokenCrm(FakeCrm):
        def email_interactions(self, email: str, since: datetime) -> list[EmailInteraction]:
            raise IntegrationError("affinity", "unavailable")

    with pytest.raises(IntegrationError):
        AffinityInboxTracker(BrokenCrm()).events_for(FOUNDER, SINCE)


def test_inbox_package_does_not_import_affinity() -> None:
    import app.integrations.inbox.affinity_tracker as module

    with open(module.__file__, encoding="utf-8") as fh:
        assert "integrations.affinity" not in fh.read()
