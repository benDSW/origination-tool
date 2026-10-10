"""R15 end to end: the real Affinity adapter (HTTP mocked by respx) feeding the real
AffinityInboxTracker and TrackingService over a real SQLite store.

The unit tests prove each piece alone; this proves they compose: a DSW outbound
email in Affinity moves DRAFTED -> SENT and the founder's later reply moves it to
REPLIED, audited as automatic. Synthetic data only; Affinity only ever sees GETs
(enforced by the ``router`` fixture's teardown).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
import respx

from app.domain.models import EventSource, OutreachRecord, OutreachStatus
from app.integrations.affinity import AffinityAdapter
from app.integrations.inbox import AffinityInboxTracker
from app.persistence import SqlStore, create_all, make_engine, make_session_factory
from app.services.origination import _founder_from_signal
from app.services.tracking import TrackingService
from tests.fakes import make_signal, utc
from tests.integrations.affinity.conftest import NOW, fixed_clock
from tests.integrations.affinity.test_adapter import (
    DSW_SENDER,
    FOUNDER,
    FOUNDER_SENDER,
    email,
    mock_interactions,
    mock_person,
    person,
)

S = OutreachStatus
DRAFTED_AT = utc(2026, 9, 1, 8, 0)


@pytest.fixture
def store() -> Iterator[SqlStore]:
    engine = make_engine("sqlite://")
    create_all(engine)
    yield SqlStore(make_session_factory(engine))
    engine.dispose()


def _drafted(store: SqlStore) -> int:
    founder = store.add_founder(_founder_from_signal(make_signal(1), DRAFTED_AT - timedelta(days=1)))
    assert founder.id is not None
    record = store.add_outreach(
        OutreachRecord(
            founder_id=founder.id,
            status=S.DRAFTED,
            email=FOUNDER,
            created_at=DRAFTED_AT,
            updated_at=DRAFTED_AT,
            drafted_at=DRAFTED_AT,
        )
    )
    assert record.id is not None
    return record.id


def test_affinity_emails_move_a_draft_to_sent_then_replied(
    router: respx.MockRouter, adapter: AffinityAdapter, store: SqlStore
) -> None:
    oid = _drafted(store)
    mock_person(router, person(42, FOUNDER))
    route = mock_interactions(
        router,
        {
            "emails": [
                email("2026-09-05T09:00:00Z", FOUNDER_SENDER, "Re: Funding Enquiry"),
                email("2026-09-02T10:00:00Z", DSW_SENDER),
            ],
            "next_page_token": None,
        },
    )

    result = TrackingService(store, AffinityInboxTracker(adapter), fixed_clock).sync()

    assert (result.checked, result.updates, result.errors) == (1, 2, [])
    # The interaction window starts at the draft time, so older mail cannot count.
    assert route.calls.last.request.url.params["start_time"] == "2026-09-01T08:00:00Z"
    saved = store.get_outreach(oid)
    assert saved.status is S.REPLIED
    assert saved.sent_at == utc(2026, 9, 2, 10, 0)
    assert saved.replied_at == utc(2026, 9, 5, 9, 0)
    assert saved.last_tracked_at == NOW
    events = store.list_status_events(oid)
    assert [(e.from_status, e.to_status, e.source) for e in events] == [
        (S.DRAFTED, S.SENT, EventSource.AUTO_INBOX),
        (S.SENT, S.REPLIED, EventSource.AUTO_INBOX),
    ]


def test_no_affinity_emails_leaves_the_draft_untouched(
    router: respx.MockRouter, adapter: AffinityAdapter, store: SqlStore
) -> None:
    oid = _drafted(store)
    mock_person(router, person(42, FOUNDER))
    mock_interactions(router, {"emails": [], "next_page_token": None})

    result = TrackingService(store, AffinityInboxTracker(adapter), fixed_clock).sync()

    assert (result.updates, result.errors) == (0, [])
    assert store.get_outreach(oid).status is S.DRAFTED


def test_affinity_outage_is_reported_without_moving_the_draft(
    router: respx.MockRouter, adapter: AffinityAdapter, store: SqlStore, sleeps: list[float]
) -> None:
    oid = _drafted(store)
    router.get("https://api.affinity.co/persons").respond(status_code=401, json={"secret": FOUNDER})

    result = TrackingService(store, AffinityInboxTracker(adapter), fixed_clock).sync()

    assert result.updates == 0
    assert len(result.errors) == 1 and result.errors[0].startswith(f"Tracking outreach {oid}: [affinity]")
    assert FOUNDER not in result.errors[0]  # no raw response body or PII in the error
    assert store.get_outreach(oid).status is S.DRAFTED
