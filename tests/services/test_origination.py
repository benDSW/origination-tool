"""OriginationService: the daily run (R1-R11) against fakes and a real SqlStore."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest

from app.domain.models import (
    CompanyRef,
    EmailLookupResult,
    EmailSource,
    EventSource,
    ExclusionReason,
    InboxEvent,
    InboxEventKind,
    KnownEntities,
    KnownEntity,
    LocationPolicy,
    LookupOutcome,
    OutreachStatus,
    RunState,
    RunTrigger,
    SearchFilter,
    Signal,
)
from app.domain.ports import IntegrationError
from app.services.origination import MAX_STORED_ERRORS, RUN_LOCK
from tests.fakes import make_signal, utc
from tests.services.conftest import LONDON, NORTH, NOW, World

S = OutreachStatus


def _status_of(world: World, signal_id: str) -> OutreachStatus:
    founder = world.store.find_founder(evertrace_signal_id=signal_id, linkedin_slug=None)
    assert founder is not None and founder.id is not None
    outreach = world.store.get_outreach_for_founder(founder.id)
    assert outreach is not None
    return outreach.status


def _outreach(world: World, signal_id: str):  # noqa: ANN202 - test helper
    founder = world.store.find_founder(evertrace_signal_id=signal_id, linkedin_slug=None)
    assert founder is not None and founder.id is not None
    outreach = world.store.get_outreach_for_founder(founder.id)
    assert outreach is not None
    return outreach


def _run(world: World, **kwargs: object):  # noqa: ANN202 - test helper
    return world.services.origination.run(RunTrigger.CRON, **kwargs)  # type: ignore[arg-type]


def _feed(world: World, saved, *signals: Signal) -> None:  # noqa: ANN001
    world.signals.by_filters[saved.filters] = list(signals)


# --------------------------------------------------------------------------- #
# R1: watermark / lookback
# --------------------------------------------------------------------------- #


def test_first_run_uses_lookback_then_watermark(world: World) -> None:
    world.enable(NORTH, lookback_days=10)
    _feed(
        world,
        NORTH,
        make_signal(1, discovered_at=utc(2026, 10, 1, 9)),
        make_signal(2, discovered_at=utc(2026, 10, 3, 9)),
        make_signal(3, discovered_at=utc(2026, 9, 1, 9)),  # older than the lookback
    )

    run = _run(world)

    assert run.state is RunState.SUCCEEDED
    assert world.signals.calls[0][1] == NOW - timedelta(days=10)
    assert run.counts.signals_seen == 2
    assert run.counts.founders_new == 2
    assert world.config(NORTH).watermark == utc(2026, 10, 3, 9)

    world.clock.advance(days=1)
    _run(world)
    assert world.signals.calls[1][1] == utc(2026, 10, 3, 9)


def test_only_enabled_searches_run(world: World) -> None:
    world.enable(NORTH)
    world.enable(LONDON, enabled=False)
    _feed(world, NORTH, make_signal(1))
    _feed(world, LONDON, make_signal(2))

    run = _run(world)

    assert [c[0] for c in world.signals.calls] == [NORTH.filters]
    assert run.counts.searches == 1


def test_catalogue_sync_registers_new_searches_disabled(world: World) -> None:
    world.catalog.searches.append(NORTH)
    run = _run(world)
    assert run.state is RunState.SUCCEEDED
    assert world.config(NORTH).enabled is False
    assert world.signals.calls == []


def test_watermark_never_moves_backwards(world: World) -> None:
    config = world.enable(NORTH)
    config.watermark = utc(2026, 10, 5)
    world.store.save_search_config(config)
    _feed(world, NORTH, make_signal(1, discovered_at=utc(2026, 10, 5)))

    _run(world)

    assert world.config(NORTH).watermark == utc(2026, 10, 5)


# --------------------------------------------------------------------------- #
# R8 / R11 / drafting
# --------------------------------------------------------------------------- #


def test_evertrace_email_is_used_for_the_draft(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1, email="alex1@CO1.example"))

    run = _run(world)

    outreach = _outreach(world, "sig_test0001")
    assert outreach.status is S.DRAFTED
    assert outreach.email == "alex1@co1.example"
    assert outreach.email_source is EmailSource.EVERTRACE
    assert outreach.draft_subject == "Funding Enquiry"
    assert outreach.draft_body is not None and outreach.draft_body.startswith("Hi Alex1,")
    assert outreach.template_version == 1
    assert run.counts.drafted == 1
    events = world.store.list_status_events(outreach.id or 0)
    assert [(e.from_status, e.to_status, e.source) for e in events] == [
        (S.DISCOVERED, S.DRAFTED, EventSource.PIPELINE)
    ]


def test_invalid_evertrace_email_means_needs_email(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1, email="not an email", linkedin_slug=None))

    run = _run(world)

    assert _status_of(world, "sig_test0001") is S.NEEDS_EMAIL
    assert run.counts.needs_email == 1
    assert world.finder.calls == []


def test_founder_record_is_built_from_the_signal(world: World) -> None:
    world.enable(NORTH)
    company = CompanyRef(
        name="Example9 Ltd", website_domain="example9.example", registry_url="https://r.example/9"
    )
    _feed(world, NORTH, make_signal(9, company=company, city="Leeds"))

    _run(world)

    founder = world.store.find_founder(evertrace_signal_id="sig_test0009", linkedin_slug=None)
    assert founder is not None
    assert (founder.company_name, founder.company_domain, founder.company_registry_url) == (
        "Example9 Ltd",
        "example9.example",
        "https://r.example/9",
    )
    assert founder.city == "Leeds"
    assert founder.created_at == NOW


# --------------------------------------------------------------------------- #
# R2: idempotent re-runs
# --------------------------------------------------------------------------- #


def test_rerun_creates_no_duplicate_founders_or_drafts(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1, email="alex1@co1.example"), make_signal(2))

    _run(world)
    config = world.config(NORTH)
    config.watermark = None  # force the same signals to be fetched again
    world.store.save_search_config(config)
    world.clock.advance(hours=1)
    second = _run(world)

    assert second.counts.founders_new == 0
    assert second.counts.drafted == 0
    assert len(world.store.list_outreach()) == 2
    outreach = _outreach(world, "sig_test0001")
    assert [e.to_status for e in world.store.list_status_events(outreach.id or 0)] == [S.DRAFTED]


def test_same_person_via_slug_across_two_searches(world: World) -> None:
    world.enable(NORTH)
    world.enable(LONDON, policy=LocationPolicy.LONDON_ALLOWED)
    _feed(world, NORTH, make_signal(1, linkedin_slug="sam-shared", email="sam@co1.example"))
    _feed(world, LONDON, make_signal(2, linkedin_slug="sam-shared", email="sam@co1.example"))

    run = _run(world)

    views = world.store.list_outreach()
    assert len(views) == 1
    assert set(views[0].search_titles) == {NORTH.title, LONDON.title}
    assert run.counts.founders_new == 1
    assert run.counts.drafted == 1


def test_founder_without_outreach_is_healed(world: World) -> None:
    from app.services.origination import _founder_from_signal

    world.store.add_founder(_founder_from_signal(make_signal(1), NOW))
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))

    run = _run(world)

    assert run.counts.founders_new == 0
    assert _status_of(world, "sig_test0001") is S.NEEDS_EMAIL


# --------------------------------------------------------------------------- #
# R3: run lock
# --------------------------------------------------------------------------- #


def test_concurrent_run_is_skipped(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))
    assert world.store.acquire_owned_lock(RUN_LOCK, owner="other-holder", now=NOW, ttl_seconds=3600)

    run = _run(world)

    assert run.state is RunState.SKIPPED
    assert run.id is not None
    assert world.signals.calls == []
    assert world.store.list_outreach() == []
    # The skipped run must not release the other run's lock.
    assert not world.store.acquire_owned_lock(RUN_LOCK, owner="other-holder", now=NOW, ttl_seconds=3600)


def test_lock_is_released_after_each_run(world: World) -> None:
    assert _run(world).state is RunState.SUCCEEDED
    assert _run(world).state is RunState.SUCCEEDED
    assert world.store.acquire_owned_lock(RUN_LOCK, owner="other-holder", now=NOW, ttl_seconds=3600)


def test_stale_lock_is_taken_over(world: World) -> None:
    assert world.store.acquire_owned_lock(
        RUN_LOCK, owner="other-holder", now=NOW - timedelta(hours=2), ttl_seconds=3600
    )
    assert _run(world).state is RunState.SUCCEEDED


def test_sync_tracking_respects_the_lock(world: World) -> None:
    assert world.store.acquire_owned_lock(RUN_LOCK, owner="other-holder", now=NOW, ttl_seconds=3600)
    assert world.services.origination.sync_tracking() is None
    world.store.release_owned_lock(RUN_LOCK, owner="other-holder")
    result = world.services.origination.sync_tracking()
    assert result is not None and result.errors == []


# --------------------------------------------------------------------------- #
# R4: failures are recorded and the run continues
# --------------------------------------------------------------------------- #


def test_failing_search_is_recorded_and_others_continue(world: World) -> None:
    world.enable(NORTH)
    world.enable(LONDON, policy=LocationPolicy.LONDON_ALLOWED)
    _feed(world, NORTH, make_signal(1))
    _feed(world, LONDON, make_signal(2, email="b@co2.example"))
    world.signals.fail_for.add(NORTH.filters)

    run = _run(world)

    assert run.state is RunState.PARTIAL
    assert any(NORTH.title in e and "[evertrace] boom" in e for e in run.errors)
    assert run.counts.errors == 1
    assert _status_of(world, "sig_test0002") is S.DRAFTED
    assert world.config(NORTH).watermark is None
    assert world.config(LONDON).watermark is not None
    assert world.store.latest_run() == run


class _FlakyStore:
    """Wraps a store; add_founder blows up for chosen signal ids."""

    def __init__(self, inner, bad_ids: set[str]) -> None:  # noqa: ANN001
        self._inner = inner
        self._bad = bad_ids

    def add_founder(self, founder):  # noqa: ANN001, ANN201
        if founder.evertrace_signal_id in self._bad:
            raise RuntimeError("synthetic failure with founder alex@co.example")
        return self._inner.add_founder(founder)

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._inner, name)


def test_one_bad_founder_does_not_abort_the_run(world: World) -> None:
    world.enable(NORTH)
    _feed(
        world,
        NORTH,
        make_signal(1, email="a@co1.example", discovered_at=utc(2026, 10, 1)),
        make_signal(2, discovered_at=utc(2026, 10, 2)),
        make_signal(3, email="c@co3.example", discovered_at=utc(2026, 10, 3)),
    )
    world.store = _FlakyStore(world.store, {"sig_test0002"})  # type: ignore[assignment]
    world.rebuild()

    run = _run(world)

    assert run.state is RunState.PARTIAL
    assert run.counts.drafted == 2
    assert run.errors == [f"Search '{NORTH.title}': signal sig_test0002 failed (RuntimeError)"]
    # No PII from the exception text reaches the run record.
    assert "alex@" not in " ".join(run.errors)
    # The watermark stops at the failed signal so it is fetched again next run.
    assert world.config(NORTH).watermark == utc(2026, 10, 2)


def test_catalogue_failure_falls_back_to_stored_searches(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))
    world.catalog.fail = True
    world.catalog.searches.clear()

    run = _run(world)

    # get_search also fails because the fake catalogue is down entirely.
    assert any("catalogue sync failed" in e for e in run.errors)
    assert run.state in (RunState.PARTIAL, RunState.FAILED)


def test_catalogue_failure_but_search_succeeds_is_partial(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))

    class _Catalog(type(world.catalog)):  # type: ignore[misc]
        def list_searches(self):  # noqa: ANN202
            raise IntegrationError("evertrace", "list failed")

    catalog = _Catalog(searches=list(world.catalog.searches))
    world.catalog = catalog
    world.rebuild()

    run = _run(world)

    assert run.state is RunState.PARTIAL
    assert run.counts.founders_new == 1


def test_search_deleted_on_evertrace_is_recorded(world: World) -> None:
    world.enable(NORTH)
    world.catalog.searches.clear()

    run = _run(world)

    assert run.errors == [f"Search '{NORTH.title}': no longer exists on Evertrace"]
    assert run.state is RunState.FAILED  # nothing else happened


def test_unexpected_error_fails_the_run_without_details(world: World) -> None:
    world.enable(NORTH)

    class _Exploding:
        def iter_signals(self, filters, since) -> Iterator[Signal]:  # noqa: ANN001
            raise RuntimeError("secret detail sk_live_123")

    world.signals = _Exploding()  # type: ignore[assignment]
    world.rebuild()

    run = _run(world)

    assert run.state is RunState.FAILED
    assert run.errors == ["Run aborted by an unexpected error (RuntimeError)"]
    assert run.finished_at is not None
    assert world.store.acquire_owned_lock(RUN_LOCK, owner="other-holder", now=NOW, ttl_seconds=3600)


def test_errors_list_is_capped(world: World) -> None:
    world.enable(NORTH)
    signals = [make_signal(i) for i in range(1, MAX_STORED_ERRORS + 6)]
    _feed(world, NORTH, *signals)
    world.store = _FlakyStore(world.store, {s.external_id for s in signals})  # type: ignore[assignment]
    world.rebuild()

    run = _run(world)

    assert len(run.errors) == MAX_STORED_ERRORS
    assert run.counts.errors == MAX_STORED_ERRORS + 5


def test_unknown_only_search_id_is_recorded(world: World) -> None:
    run = _run(world, only_search_id=999)
    assert run.errors == ["Search 999 not found"]


def test_only_search_id_runs_that_search_even_if_disabled(world: World) -> None:
    world.enable(NORTH)
    london = world.enable(LONDON, enabled=False, policy=LocationPolicy.LONDON_ALLOWED)
    _feed(world, NORTH, make_signal(1))
    _feed(world, LONDON, make_signal(2))

    run = _run(world, only_search_id=london.id)

    assert [c[0] for c in world.signals.calls] == [LONDON.filters]
    assert run.counts.searches == 1


# --------------------------------------------------------------------------- #
# R5: location policy per search (+ rescue by a more permissive search)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("city", "country", "eligible"),
    [
        ("Manchester", "United Kingdom", True),
        ("London", "United Kingdom", False),
        ("Croydon", "United Kingdom", False),
        ("Paris", "France", False),
        (None, "United Kingdom", True),
    ],
)
def test_default_policy_excludes_london_m25_and_non_uk(
    world: World, city: str | None, country: str, eligible: bool
) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1, city=city, country=country))

    run = _run(world)

    outreach = _outreach(world, "sig_test0001")
    if eligible:
        assert outreach.status is S.NEEDS_EMAIL
    else:
        assert outreach.status is S.EXCLUDED
        assert outreach.exclusion_reason is ExclusionReason.LOCATION
        assert outreach.exclusion_detail
        assert run.counts.excluded_location == 1


def test_london_allowed_policy_admits_london(world: World) -> None:
    world.enable(LONDON, policy=LocationPolicy.LONDON_ALLOWED)
    _feed(
        world, LONDON, make_signal(1, city="London", email="a@co1.example"), make_signal(2, country="Spain")
    )

    _run(world)

    assert _status_of(world, "sig_test0001") is S.DRAFTED
    assert _status_of(world, "sig_test0002") is S.EXCLUDED


def test_admitted_founder_is_not_excluded_by_a_later_search_in_the_same_run(world: World) -> None:
    # "A North" (strict) sorts first; give the permissive policy to the first search instead.
    world.enable(NORTH, policy=LocationPolicy.LONDON_ALLOWED)
    world.enable(LONDON, policy=LocationPolicy.UK_OUTSIDE_LONDON)
    signal = make_signal(1, city="London", email="a@co1.example")
    _feed(world, NORTH, signal)
    _feed(world, LONDON, signal)

    run = _run(world)

    assert _status_of(world, "sig_test0001") is S.DRAFTED
    assert run.counts.excluded_location == 0


def test_location_exclusion_is_rescued_by_a_permissive_search(world: World) -> None:
    world.enable(NORTH)  # strict, runs first
    world.enable(LONDON, policy=LocationPolicy.LONDON_ALLOWED)
    signal = make_signal(1, city="London", email="a@co1.example")
    _feed(world, NORTH, signal)
    _feed(world, LONDON, signal)

    run = _run(world)

    outreach = _outreach(world, "sig_test0001")
    assert outreach.status is S.DRAFTED
    assert outreach.exclusion_reason is None
    events = world.store.list_status_events(outreach.id or 0)
    assert [(e.from_status, e.to_status) for e in events] == [
        (S.DISCOVERED, S.EXCLUDED),
        (S.EXCLUDED, S.DRAFTED),
    ]
    assert run.counts.excluded_location == 1
    assert run.counts.drafted == 1


def test_rescued_founder_without_email_goes_to_needs_email(world: World) -> None:
    world.enable(NORTH)
    world.enable(LONDON, policy=LocationPolicy.LONDON_ALLOWED)
    signal = make_signal(1, city="London", linkedin_slug=None)
    _feed(world, NORTH, signal)
    _feed(world, LONDON, signal)

    _run(world)

    assert _status_of(world, "sig_test0001") is S.NEEDS_EMAIL


def test_rescued_founder_in_affinity_only_changes_reason(world: World) -> None:
    world.enable(NORTH)
    world.enable(LONDON, policy=LocationPolicy.LONDON_ALLOWED)
    signal = make_signal(1, city="London", email="a@co1.example")
    _feed(world, NORTH, signal)
    _feed(world, LONDON, signal)
    world.crm.known = KnownEntities(
        entities=(KnownEntity(list_name="Origination Opportunities", opportunity_name="Example1"),)
    )

    run = _run(world)

    outreach = _outreach(world, "sig_test0001")
    assert outreach.status is S.EXCLUDED
    assert outreach.exclusion_reason is ExclusionReason.IN_AFFINITY
    assert "Origination Opportunities" in (outreach.exclusion_detail or "")
    assert len(world.store.list_status_events(outreach.id or 0)) == 1  # no event for the reason change
    assert run.counts.excluded_affinity == 1


def test_rescue_waits_for_affinity_and_holds_the_watermark(world: World) -> None:
    world.enable(NORTH)
    world.enable(LONDON, policy=LocationPolicy.LONDON_ALLOWED)
    signal = make_signal(1, city="London", email="a@co1.example")
    _feed(world, NORTH, signal)
    _feed(world, LONDON, signal)
    world.crm.fail = True

    first = _run(world)

    assert first.state is RunState.PARTIAL
    assert _status_of(world, "sig_test0001") is S.EXCLUDED
    assert world.config(LONDON).watermark is None  # held so the founder is seen again
    assert world.config(NORTH).watermark == signal.discovered_at

    world.crm.fail = False
    world.clock.advance(days=1)
    second = _run(world)

    assert second.state is RunState.SUCCEEDED
    assert _status_of(world, "sig_test0001") is S.DRAFTED


# --------------------------------------------------------------------------- #
# R6: Affinity dedupe
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("entity", "matched_on"),
    [
        (
            KnownEntity(list_name="Applicant Opportunities", opportunity_name="Example1 Limited"),
            "company name",
        ),
        (
            KnownEntity(
                list_name="Origination Opportunities",
                opportunity_name="Other",
                company_domains=("https://www.example1.example",),
            ),
            "company domain",
        ),
        (
            KnownEntity(
                list_name="Origination Opportunities",
                opportunity_name="Other",
                person_names=("Alex1 Testfounder",),
            ),
            "person name",
        ),
    ],
)
def test_founder_known_in_affinity_is_excluded_with_reason(
    world: World, entity: KnownEntity, matched_on: str
) -> None:
    world.enable(NORTH)
    company = CompanyRef(name="Example1 Ltd", website_domain="example1.example")
    _feed(world, NORTH, make_signal(1, email="a@co1.example", company=company))
    world.crm.known = KnownEntities(entities=(entity,))

    run = _run(world)

    outreach = _outreach(world, "sig_test0001")
    assert outreach.status is S.EXCLUDED
    assert outreach.exclusion_reason is ExclusionReason.IN_AFFINITY
    assert entity.list_name in (outreach.exclusion_detail or "")
    assert f"matched on {matched_on}" in (outreach.exclusion_detail or "")
    assert run.counts.excluded_affinity == 1
    assert run.counts.drafted == 0


def test_crm_outage_leaves_founders_discovered_until_next_run(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1, email="a@co1.example"))
    world.crm.fail = True

    first = _run(world)

    assert first.state is RunState.PARTIAL
    assert any("Affinity dedupe unavailable" in e and "[affinity] unavailable" in e for e in first.errors)
    assert _status_of(world, "sig_test0001") is S.DISCOVERED
    # Discovered founders are retried from the DB, so the watermark can advance.
    assert world.config(NORTH).watermark is not None

    world.crm.fail = False
    world.clock.advance(days=1)
    second = _run(world)

    assert second.state is RunState.SUCCEEDED
    assert second.counts.drafted == 1
    assert _status_of(world, "sig_test0001") is S.DRAFTED


def test_no_crm_call_when_nothing_to_dedupe(world: World) -> None:
    world.crm.fail = True
    assert _run(world).state is RunState.SUCCEEDED


def test_draft_step_failure_for_one_founder_is_recorded(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1, email="a@co1.example"), make_signal(2, email="b@co2.example"))

    class _Store(_FlakyStore):
        def save_outreach(self, outreach, event=None):  # noqa: ANN001, ANN201
            if event is not None and event.to_status is S.DRAFTED and outreach.founder_id == 1:
                raise RuntimeError("db hiccup")
            return self._inner.save_outreach(outreach, event)

    world.store = _Store(world.store, set())  # type: ignore[assignment]
    world.rebuild()

    run = _run(world)

    assert run.state is RunState.PARTIAL
    assert run.counts.drafted == 1
    assert any("draft step failed (RuntimeError)" in e for e in run.errors)


# --------------------------------------------------------------------------- #
# R9 / R10: RocketReach backlog
# --------------------------------------------------------------------------- #


def _found(email: str, grade: str | None = "A") -> EmailLookupResult:
    return EmailLookupResult(outcome=LookupOutcome.FOUND, email=email, grade=grade)


def test_rocketreach_only_for_needs_email_with_slug(world: World) -> None:
    world.enable(NORTH)
    _feed(
        world,
        NORTH,
        make_signal(1),  # needs email, has slug
        make_signal(2, linkedin_slug=None),  # needs email, no slug
        make_signal(3, email="c@co3.example"),  # has an Evertrace email
        make_signal(4, city="London"),  # excluded
    )
    world.finder.results["alex-testfounder-1"] = _found("alex1@co1.example")

    run = _run(world)

    assert world.finder.calls == ["alex-testfounder-1"]
    outreach = _outreach(world, "sig_test0001")
    assert outreach.status is S.DRAFTED
    assert outreach.email_source is EmailSource.ROCKETREACH
    assert outreach.email == "alex1@co1.example"
    assert _status_of(world, "sig_test0002") is S.NEEDS_EMAIL
    assert run.counts.rocketreach_lookups == 1
    assert run.counts.drafted == 2
    founder = world.store.find_founder(evertrace_signal_id="sig_test0001", linkedin_slug=None)
    assert founder is not None and founder.id is not None
    cached = world.store.get_email_lookup(founder.id, EmailSource.ROCKETREACH)
    assert cached is not None and cached.outcome is LookupOutcome.FOUND and cached.grade == "A"


def test_rocketreach_never_called_twice_for_a_founder(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))

    _run(world)
    world.clock.advance(days=1)
    second = _run(world)

    assert world.finder.calls == ["alex-testfounder-1"]  # NOT_FOUND is cached
    assert second.counts.rocketreach_lookups == 0
    assert _status_of(world, "sig_test0001") is S.NEEDS_EMAIL


def test_rocketreach_backlog_is_oldest_first(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))
    _run(world, use_rocketreach=False)
    world.clock.advance(hours=1)
    _feed(world, NORTH, make_signal(1), make_signal(2))
    world.rebuild(rocketreach_daily_cap=1)

    _run(world)

    assert world.finder.calls == ["alex-testfounder-1"]


def test_daily_cap_is_respected_across_runs(world: World) -> None:
    world.rebuild(rocketreach_daily_cap=2)
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1), make_signal(2), make_signal(3))

    first = _run(world)
    assert first.counts.rocketreach_lookups == 2

    world.clock.advance(hours=2)
    second = _run(world)
    assert second.counts.rocketreach_lookups == 0

    world.clock.advance(days=1)  # new UTC day
    third = _run(world)
    assert third.counts.rocketreach_lookups == 1
    assert len(world.finder.calls) == 3
    assert len(set(world.finder.calls)) == 3


def test_zero_daily_cap_disables_rocketreach(world: World) -> None:
    world.rebuild(rocketreach_daily_cap=0)
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))
    _run(world)
    assert world.finder.calls == []


def test_exhausted_credits_skip_lookups(world: World) -> None:
    world.finder.credits = 0
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))

    run = _run(world)

    assert world.finder.calls == []
    assert "RocketReach credits exhausted; email lookups skipped" in run.errors
    assert run.state is RunState.PARTIAL


def test_unknown_credits_still_allow_lookups(world: World) -> None:
    world.finder.credits = None
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))
    _run(world)
    assert world.finder.calls == ["alex-testfounder-1"]


def test_credit_check_errors_are_tolerated(world: World) -> None:
    class _Finder(type(world.finder)):  # type: ignore[misc]
        def remaining_credits(self) -> int | None:
            raise IntegrationError("rocketreach", "down")

    finder = _Finder()
    world.rebuild(finder=finder)
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))

    _run(world)

    assert finder.calls == ["alex-testfounder-1"]


def test_three_consecutive_errors_stop_rocketreach_and_errors_are_retried(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, *[make_signal(i) for i in range(1, 6)])
    world.finder.raise_for = {f"alex-testfounder-{i}" for i in range(1, 6)}

    first = _run(world)

    assert world.finder.calls == ["alex-testfounder-1", "alex-testfounder-2", "alex-testfounder-3"]
    assert first.counts.rocketreach_lookups == 3
    assert any("after 3 consecutive errors" in e for e in first.errors)
    assert first.state is RunState.PARTIAL
    founder = world.store.find_founder(evertrace_signal_id="sig_test0001", linkedin_slug=None)
    assert founder is not None and founder.id is not None
    cached = world.store.get_email_lookup(founder.id, EmailSource.ROCKETREACH)
    assert cached is not None and cached.outcome is LookupOutcome.ERROR
    assert cached.reason == "[rocketreach] timeout"

    # ERROR rows are retried on a later run.
    world.finder.raise_for = set()
    world.finder.results["alex-testfounder-1"] = _found("alex1@co1.example")
    world.finder.calls.clear()
    world.clock.advance(days=1)
    second = _run(world)

    assert world.finder.calls[0] == "alex-testfounder-1"
    assert len(world.finder.calls) == 5
    assert second.counts.drafted == 1


def test_error_streak_resets_after_a_success(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, *[make_signal(i) for i in range(1, 6)])
    world.finder.raise_for = {"alex-testfounder-1", "alex-testfounder-2", "alex-testfounder-4"}

    _run(world)

    assert len(world.finder.calls) == 5


def test_non_retryable_error_stops_rocketreach_immediately(world: World) -> None:
    class _Finder(type(world.finder)):  # type: ignore[misc]
        def find_email(self, *, linkedin_slug: str, full_name: str, company_domain: str | None):  # noqa: ANN201
            self.calls.append(linkedin_slug)
            raise IntegrationError("rocketreach", "API key is not configured", retryable=False)

    finder = _Finder()
    world.rebuild(finder=finder)
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1), make_signal(2))

    run = _run(world)

    assert finder.calls == ["alex-testfounder-1"]
    assert "RocketReach lookups stopped for this run (non-retryable error)" in run.errors


def test_found_but_invalid_email_stays_needs_email(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))
    world.finder.results["alex-testfounder-1"] = _found("broken@@address")

    _run(world)

    assert _status_of(world, "sig_test0001") is S.NEEDS_EMAIL


def test_found_with_no_grade_is_drafted(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))
    world.finder.results["alex-testfounder-1"] = _found("alex1@co1.example", grade=None)
    _run(world)
    assert _status_of(world, "sig_test0001") is S.DRAFTED


def test_lookup_step_failure_is_recorded_and_stops_paid_lookups(world: World) -> None:
    """Security review #1: a failure *after* a paid call (here: recording it) ends the
    RocketReach phase for the run instead of moving on to the next founder, and the
    attempt still counts against the cap and is never repeated."""
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1), make_signal(2))

    class _Store(_FlakyStore):
        def add_email_lookup(self, record):  # noqa: ANN001, ANN201
            if record.founder_id == 1:
                raise RuntimeError("db hiccup")
            return self._inner.add_email_lookup(record)

    world.store = _Store(world.store, set())  # type: ignore[assignment]
    world.rebuild()

    run = _run(world)

    assert world.finder.calls == ["alex-testfounder-1"]
    assert run.counts.rocketreach_lookups == 1
    assert any("email lookup step failed (RuntimeError)" in e for e in run.errors)
    assert any("RocketReach lookups stopped for this run" in e for e in run.errors)
    # The reservation made before the call stays: founder 1 is never looked up again.
    world.clock.advance(days=1)
    _run(world)
    assert world.finder.calls == ["alex-testfounder-1", "alex-testfounder-2"]


def test_no_rocketreach_flag_and_missing_finder(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1))
    _run(world, use_rocketreach=False)
    assert world.finder.calls == []

    world.rebuild(finder=None)
    world.clock.advance(days=1)
    assert _run(world).state is RunState.SUCCEEDED
    assert world.finder.calls == []


# --------------------------------------------------------------------------- #
# Dry run + tracking
# --------------------------------------------------------------------------- #


def test_dry_run_has_no_side_effects_on_watermarks_rocketreach_or_tracking(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1), make_signal(2, email="b@co2.example"))

    run = _run(world, dry_run=True)

    assert run.dry_run is True
    assert run.state is RunState.SUCCEEDED
    assert world.config(NORTH).watermark is None
    assert world.finder.calls == []
    assert world.inbox.calls == []
    assert run.counts.drafted == 1  # drafts are still recorded in the tool's own DB
    assert run.counts.needs_email == 1


def test_run_includes_tracking(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1, email="a@co1.example"))
    _run(world)
    world.inbox.events["a@co1.example"] = [
        InboxEvent(kind=InboxEventKind.SENT, occurred_at=NOW + timedelta(hours=1))
    ]
    world.clock.advance(days=1)

    run = _run(world)

    assert run.counts.tracking_updates == 1
    assert _status_of(world, "sig_test0001") is S.SENT


def test_tracking_errors_are_added_to_the_run(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1, email="a@co1.example"))
    _run(world)
    world.inbox.fail = True
    world.clock.advance(days=1)

    run = _run(world)

    assert run.state is RunState.PARTIAL
    assert any("[inbox] unavailable" in e for e in run.errors)


def test_run_records_trigger_and_times(world: World) -> None:
    run = world.services.origination.run(RunTrigger.MANUAL)
    assert run.trigger is RunTrigger.MANUAL
    assert run.started_at == NOW
    assert run.finished_at == NOW
    assert world.store.list_runs()[0].id == run.id


def test_unused_filter_tuple_is_independent_of_search_identity(world: World) -> None:
    """Filters come from get_search (current definition), not from the stored config."""
    updated = NORTH.model_copy(update={"filters": (SearchFilter(key="country", values=("Ireland",)),)})
    world.enable(NORTH)
    world.catalog.searches[:] = [updated]
    _run(world)
    assert world.signals.calls[0][0] == updated.filters


def test_watermark_save_failure_is_recorded(world: World) -> None:
    world.enable(NORTH)
    _feed(world, NORTH, make_signal(1, email="a@co1.example"))

    class _Store(_FlakyStore):
        def save_search_config(self, config):  # noqa: ANN001, ANN201
            raise RuntimeError("db hiccup")

    world.store = _Store(world.store, set())  # type: ignore[assignment]
    world.rebuild()

    run = _run(world)

    assert run.state is RunState.PARTIAL
    assert run.errors == [f"Search '{NORTH.title}': watermark not saved (RuntimeError)"]
    assert run.counts.drafted == 1
