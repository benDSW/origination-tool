"""OutreachService: dashboard actions (R7, R11, R16, R17)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.models import (
    CloseReason,
    EmailLookupRecord,
    EmailLookupResult,
    EmailSource,
    EventSource,
    ExclusionReason,
    LookupOutcome,
    OutreachStatus,
    RunTrigger,
)
from app.domain.ports import IntegrationError
from app.services import InvalidAction, NotFound, ValidationFailed
from app.services.outreach import CREDITS_CACHE_SECONDS, OutreachService
from tests.fakes import FakeEmailFinder, make_signal
from tests.services.conftest import NORTH, NOW, World

S = OutreachStatus


def _seed(world: World, *signals) -> dict[str, int]:  # noqa: ANN002
    """Run the pipeline over these signals; return signal id -> outreach id."""
    world.enable(NORTH)
    world.signals.by_filters[NORTH.filters] = list(signals)
    world.services.origination.run(RunTrigger.CRON, use_rocketreach=False)
    ids: dict[str, int] = {}
    for view in world.store.list_outreach():
        ids[view.founder.evertrace_signal_id] = view.outreach.id or 0
    return ids


@pytest.fixture
def ids(world: World) -> dict[str, int]:
    return _seed(
        world,
        make_signal(1, email="a@co1.example"),  # DRAFTED
        make_signal(2),  # NEEDS_EMAIL
        make_signal(3, city="London", email="c@co3.example"),  # EXCLUDED (location), has email
        make_signal(4, city="London"),  # EXCLUDED, no email
    )


def _svc(world: World) -> OutreachService:
    return world.services.outreach


# --------------------------------------------------------------------------- #
# R16: manual transitions
# --------------------------------------------------------------------------- #


def test_manual_sent_replied_deck_close(world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0001"]
    svc = _svc(world)

    assert svc.mark_sent(oid).status is S.SENT
    world.clock.advance(days=2)
    assert svc.mark_replied(oid).status is S.REPLIED
    assert svc.mark_deck_received(oid).status is S.DECK_RECEIVED
    closed = svc.close(oid, "declined")
    assert closed.status is S.CLOSED
    assert closed.close_reason is CloseReason.DECLINED

    events = world.store.list_status_events(oid)
    assert [e.to_status for e in events] == [S.DRAFTED, S.SENT, S.REPLIED, S.DECK_RECEIVED, S.CLOSED]
    assert all(e.source is EventSource.MANUAL for e in events[1:])
    record = world.store.get_outreach(oid)
    assert record.sent_at == NOW
    assert record.replied_at == NOW + timedelta(days=2)


def test_invalid_transitions_are_rejected_with_a_safe_message(world: World, ids: dict[str, int]) -> None:
    svc = _svc(world)
    with pytest.raises(InvalidAction, match="Cannot move from needs email to sent"):
        svc.mark_sent(ids["sig_test0002"])
    with pytest.raises(InvalidAction):
        svc.mark_deck_received(ids["sig_test0001"])
    svc.close(ids["sig_test0001"])
    with pytest.raises(InvalidAction):
        svc.close(ids["sig_test0001"])
    # Nothing was recorded for the rejected moves.
    assert [e.to_status for e in world.store.list_status_events(ids["sig_test0002"])] == [S.NEEDS_EMAIL]


def test_close_defaults_to_skipped_and_rejects_unknown_reasons(world: World, ids: dict[str, int]) -> None:
    svc = _svc(world)
    with pytest.raises(ValidationFailed):
        svc.close(ids["sig_test0002"], "bored")
    assert svc.close(ids["sig_test0002"]).close_reason is CloseReason.SKIPPED


def test_missing_outreach_raises_not_found(world: World) -> None:
    svc = _svc(world)
    for action in (svc.mark_sent, svc.mark_replied, svc.discard_email, svc.draft_anyway, svc.detail):
        with pytest.raises(NotFound):
            action(12345)
    with pytest.raises(NotFound):
        svc.set_manual_email(12345, "x@co.example")


def test_outreach_deleted_between_read_and_save_raises_not_found(world: World, ids: dict[str, int]) -> None:
    svc = _svc(world)
    oid = ids["sig_test0001"]
    record = world.store.get_outreach(oid)
    world.store.delete_founder(record.founder_id)
    with pytest.raises(NotFound):
        svc._move(oid, S.SENT, outreach=record)


# --------------------------------------------------------------------------- #
# R11: manual email; R7: exclusions overridable
# --------------------------------------------------------------------------- #


def test_manual_email_drafts_a_needs_email_founder(world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0002"]

    record = _svc(world).set_manual_email(oid, "  alex2@CO2.example ")

    assert record.status is S.DRAFTED
    assert record.email == "alex2@co2.example"
    assert record.email_source is EmailSource.MANUAL
    assert record.draft_subject == "Funding Enquiry"
    assert record.draft_body is not None and "Hi Alex2" in record.draft_body
    assert world.store.list_status_events(oid)[-1].source is EventSource.MANUAL


@pytest.mark.parametrize("bad", ["", "nope", "a@b.example\r\nBcc: x@y.example", "a@b.example,c@d.example"])
def test_manual_email_is_validated(world: World, ids: dict[str, int], bad: str) -> None:
    with pytest.raises(ValidationFailed, match="not valid"):
        _svc(world).set_manual_email(ids["sig_test0002"], bad)
    assert world.store.get_outreach(ids["sig_test0002"]).status is S.NEEDS_EMAIL


def test_manual_email_overrides_an_exclusion(world: World, ids: dict[str, int]) -> None:
    record = _svc(world).set_manual_email(ids["sig_test0004"], "d@co4.example")
    assert record.status is S.DRAFTED
    assert record.exclusion_reason is None


def test_manual_email_rejected_for_other_statuses(world: World, ids: dict[str, int]) -> None:
    with pytest.raises(InvalidAction):
        _svc(world).set_manual_email(ids["sig_test0001"], "z@co1.example")


def test_draft_anyway_uses_the_evertrace_email(world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0003"]
    assert world.store.get_outreach(oid).exclusion_reason is ExclusionReason.LOCATION

    record = _svc(world).draft_anyway(oid)

    assert record.status is S.DRAFTED
    assert record.email == "c@co3.example"
    assert record.email_source is EmailSource.EVERTRACE
    assert world.store.list_status_events(oid)[-1].note == "Exclusion overridden (location)"


def test_draft_anyway_uses_a_cached_rocketreach_email(world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0004"]
    founder_id = world.store.get_outreach(oid).founder_id
    world.store.add_email_lookup(
        EmailLookupRecord(
            founder_id=founder_id,
            provider=EmailSource.ROCKETREACH,
            outcome=LookupOutcome.FOUND,
            email="d@co4.example",
            grade="A",
            created_at=NOW,
        )
    )

    record = _svc(world).draft_anyway(oid)

    assert (record.status, record.email_source) == (S.DRAFTED, EmailSource.ROCKETREACH)


def test_draft_anyway_without_email_goes_to_needs_email(world: World, ids: dict[str, int]) -> None:
    record = _svc(world).draft_anyway(ids["sig_test0004"])
    assert record.status is S.NEEDS_EMAIL
    assert record.exclusion_reason is None


def test_draft_anyway_only_for_excluded(world: World, ids: dict[str, int]) -> None:
    with pytest.raises(InvalidAction):
        _svc(world).draft_anyway(ids["sig_test0002"])


def test_discard_email_returns_to_needs_email(world: World, ids: dict[str, int]) -> None:
    record = _svc(world).discard_email(ids["sig_test0001"])
    assert record.status is S.NEEDS_EMAIL
    assert record.email is None
    assert record.draft_body is None
    with pytest.raises(InvalidAction):
        _svc(world).discard_email(ids["sig_test0002"])


def test_forget_founder_erases_everything(world: World, ids: dict[str, int]) -> None:
    svc = _svc(world)
    founder_id = world.store.get_outreach(ids["sig_test0001"]).founder_id

    svc.forget_founder(founder_id)

    assert world.store.find_founder(evertrace_signal_id="sig_test0001", linkedin_slug=None) is None
    with pytest.raises(NotFound):
        svc.forget_founder(founder_id)

    svc.forget_outreach_founder(ids["sig_test0002"])
    assert len(world.store.list_outreach()) == 2


# --------------------------------------------------------------------------- #
# Reads, R17 follow-ups, summary
# --------------------------------------------------------------------------- #


def test_detail_includes_events_draft_and_lookup(world: World, ids: dict[str, int]) -> None:
    founder_id = world.store.get_outreach(ids["sig_test0002"]).founder_id
    world.store.add_email_lookup(
        EmailLookupRecord(
            founder_id=founder_id,
            provider=EmailSource.ROCKETREACH,
            outcome=LookupOutcome.REJECTED,
            reason="slug mismatch",
            created_at=NOW,
        )
    )
    svc = _svc(world)

    drafted = svc.detail(ids["sig_test0001"])
    assert drafted.draft is not None
    assert drafted.draft.to == "a@co1.example"
    assert drafted.draft.as_text().startswith("To: a@co1.example\nSubject: Funding Enquiry\n\nHi Alex1,")
    assert [e.to_status for e in drafted.events] == [S.DRAFTED]
    assert drafted.founder.first_name == "Alex1"
    assert drafted.outreach.status is S.DRAFTED
    assert drafted.item.days_waiting == 0

    needs = svc.detail(ids["sig_test0002"])
    assert needs.draft is None
    assert needs.rocketreach_lookup is not None
    assert needs.rocketreach_lookup.reason == "slug mismatch"


def test_list_by_status(world: World, ids: dict[str, int]) -> None:
    svc = _svc(world)
    assert {i.founder.evertrace_signal_id for i in svc.list_by_status([S.EXCLUDED])} == {
        "sig_test0003",
        "sig_test0004",
    }
    assert len(svc.list_by_status()) == 4
    assert svc.list_by_status([S.CLOSED]) == []


def test_follow_up_flag_and_days_waiting(world: World, ids: dict[str, int]) -> None:
    svc = _svc(world)
    svc.mark_sent(ids["sig_test0001"])
    svc.set_manual_email(ids["sig_test0002"], "b@co2.example")
    world.clock.advance(days=5)
    svc.mark_sent(ids["sig_test0002"])

    world.clock.advance(days=13)  # first: 18 days since sent; second: 13 days

    sent = {i.founder.evertrace_signal_id: i for i in svc.list_by_status([S.SENT])}
    assert sent["sig_test0001"].days_waiting == 18
    assert sent["sig_test0001"].follow_up_due is True
    assert sent["sig_test0002"].days_waiting == 13
    assert sent["sig_test0002"].follow_up_due is False
    assert [i.founder.evertrace_signal_id for i in svc.follow_ups_due()] == ["sig_test0001"]

    world.clock.advance(days=1)  # exactly 14 days -> due
    assert len(svc.follow_ups_due()) == 2


def test_pipeline_columns(world: World, ids: dict[str, int]) -> None:
    svc = _svc(world)
    svc.mark_sent(ids["sig_test0001"])
    svc.set_manual_email(ids["sig_test0002"], "b@co2.example")
    svc.mark_sent(ids["sig_test0002"])
    world.clock.advance(days=1)
    svc.mark_replied(ids["sig_test0002"])
    world.clock.advance(days=3)

    board = svc.pipeline()

    assert list(board.columns) == [S.SENT, S.REPLIED, S.DECK_RECEIVED]
    assert [i.days_waiting for i in board.columns[S.SENT]] == [4]
    assert [i.days_waiting for i in board.columns[S.REPLIED]] == [3]
    assert board.columns[S.DECK_RECEIVED] == []
    assert board.columns[S.SENT][0].draft is None


def test_dashboard_summary(world: World, ids: dict[str, int]) -> None:
    world.finder.credits = 42
    svc = _svc(world)
    svc.mark_sent(ids["sig_test0001"])
    world.clock.advance(days=20)

    summary = svc.dashboard_summary()

    assert summary.counts[S.SENT] == 1
    assert summary.counts[S.EXCLUDED] == 2
    assert summary.counts[S.CLOSED] == 0
    assert summary.latest_run is not None
    assert summary.rocketreach_credits == 42
    assert [i.days_waiting for i in summary.follow_ups_due] == [20]
    assert summary.follow_up_after_days == 14
    assert summary.generated_at == world.clock()


def test_credits_are_cached_briefly(world: World) -> None:
    world.finder.credits = 10
    svc = _svc(world)
    assert svc.dashboard_summary().rocketreach_credits == 10
    world.finder.credits = 9
    assert svc.dashboard_summary().rocketreach_credits == 10
    world.clock.advance(seconds=CREDITS_CACHE_SECONDS + 1)
    assert svc.dashboard_summary().rocketreach_credits == 9


def test_credits_none_without_finder_or_on_error(world: World) -> None:
    assert world.rebuild(finder=None).outreach.dashboard_summary().rocketreach_credits is None

    class _Broken(FakeEmailFinder):
        def remaining_credits(self) -> int | None:
            raise IntegrationError("rocketreach", "down")

    assert world.rebuild(finder=_Broken()).outreach.dashboard_summary().rocketreach_credits is None


def test_rerender_open_drafts_after_template_change(world: World, ids: dict[str, int]) -> None:
    svc = _svc(world)
    svc.set_manual_email(ids["sig_test0002"], "b@co2.example")
    svc.mark_sent(ids["sig_test0002"])  # sent drafts are history; never re-rendered

    template, changed = world.services.save_template(
        "Hello {first_name}", "Dear {full_name}, at {company_name}."
    )

    assert template.version == 2
    assert changed == 1
    drafted = world.store.get_outreach(ids["sig_test0001"])
    assert drafted.draft_subject == "Hello Alex1"
    assert drafted.draft_body == "Dear Alex1 Testfounder, at Example1 Limited."
    assert drafted.template_version == 2
    sent = world.store.get_outreach(ids["sig_test0002"])
    assert sent.draft_subject == "Funding Enquiry"
    assert svc.rerender_open_drafts() == 0  # already current


def test_rerender_skips_undeliverable_addresses(world: World, ids: dict[str, int]) -> None:
    record = world.store.get_outreach(ids["sig_test0001"])
    record.email = "broken@@address"
    world.store.save_outreach(record)
    world.store.save_template("New {first_name}", "Body")
    assert _svc(world).rerender_open_drafts() == 0


def test_found_lookup_result_type_is_exported() -> None:
    # Guard: the services rely on FOUND results possibly having no grade.
    assert EmailLookupResult(outcome=LookupOutcome.FOUND, email="x@co.example").grade is None
