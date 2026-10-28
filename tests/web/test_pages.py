"""Dashboard pages and actions (R7, R11, R13, R14, R16, R17, R18, R19)."""

from __future__ import annotations

import html
import re
from datetime import timedelta

import pytest

from app.domain.models import (
    CloseReason,
    CompanyRef,
    EmailSource,
    InboxEvent,
    InboxEventKind,
    LocationPolicy,
    OutreachStatus,
    RunState,
    RunTrigger,
    SavedSearch,
    SearchFilter,
)
from app.domain.ports import IntegrationError
from app.services.origination import RUN_LOCK
from tests.fakes import make_signal
from tests.web.conftest import NOW, WebClient, World

S = OutreachStatus


def textarea(page: str, element_id: str) -> str:
    """The value a browser would see in <textarea id=...> (first newline dropped)."""
    match = re.search(rf'<textarea id="{element_id}"[^>]*>(.*?)</textarea>', page, re.S)
    assert match, f"textarea {element_id} not found"
    value = html.unescape(match.group(1))
    return value[1:] if value.startswith("\n") else value


def input_value(page: str, element_id: str) -> str:
    match = re.search(rf'<input id="{element_id}"[^>]*value="([^"]*)"', page)
    assert match, f"input {element_id} not found"
    return html.unescape(match.group(1))


# --------------------------------------------------------------------------- #
# Overview, Run now, Sync tracking, Runs
# --------------------------------------------------------------------------- #


def test_overview_shows_counts_latest_run_credits_and_buttons(client: WebClient, ids: dict[str, int]) -> None:
    page = client.get("/").text
    assert '<td data-status="drafted">1</td>' in page
    assert '<td data-status="needs_email">1</td>' in page
    assert '<td data-status="excluded">2</td>' in page
    assert '<td data-status="sent">0</td>' in page
    assert "succeeded" in page  # latest run state
    assert "Signals 4" in page
    assert '<p class="big">100</p>' in page  # FakeEmailFinder credits
    assert 'action="/runs"' in page and 'action="/tracking/sync"' in page


def test_overview_without_runs_or_credits(client: WebClient, world: World) -> None:
    world.finder.credits = None
    world.clock.advance(minutes=6)  # credits are cached for 5 minutes
    page = client.get("/").text
    assert "No runs yet." in page
    assert "unknown" in page
    assert "Nothing due." in page


def test_run_now_starts_a_manual_run_in_the_background(client: WebClient, world: World) -> None:
    response = client.post("/runs")
    assert response.status_code == 303 and response.headers["location"] == "/"
    latest = world.store.latest_run()
    assert latest is not None and latest.trigger is RunTrigger.MANUAL
    assert latest.state is RunState.SUCCEEDED
    assert "Run started" in client.flash(response)


def test_run_now_is_skipped_while_another_run_holds_the_lock(client: WebClient, world: World) -> None:
    assert world.store.acquire_owned_lock(RUN_LOCK, owner="other-holder", now=NOW, ttl_seconds=3600)
    client.post("/runs")
    latest = world.store.latest_run()
    assert latest is not None and latest.state is RunState.SKIPPED


def test_run_now_survives_an_unexpected_error(
    client: WebClient, world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic-detail-not-for-logs")

    monkeypatch.setattr(world.services.origination, "run", boom)
    response = client.post("/runs")
    assert response.status_code == 303
    assert "Manual run failed (RuntimeError)" in caplog.text
    assert "synthetic-detail-not-for-logs" not in caplog.text


def test_sync_tracking_button_moves_drafted_to_sent(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    world.inbox.events["alex1@co1.example"] = [InboxEvent(kind=InboxEventKind.SENT, occurred_at=NOW)]
    response = client.post("/tracking/sync")
    assert world.status(ids["sig_test0001"]) is S.SENT
    assert "1 status update(s)" in client.flash(response)


def test_check_now_on_pipeline_forces_a_full_check_and_returns_there(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    oid = ids["sig_test0001"]
    _sent(client, world, oid)
    page = client.get("/pipeline").text
    assert 'action="/tracking/sync"' in page
    # Sent long ago and checked an hour ago: a timer tick would skip it, the button must not.
    record = world.store.get_outreach(oid)
    record.sent_at = NOW - timedelta(days=40)
    record.last_tracked_at = NOW - timedelta(hours=1)
    world.store.save_outreach(record)
    world.inbox.events["alex1@co1.example"] = [InboxEvent(kind=InboxEventKind.REPLIED, occurred_at=NOW)]

    response = client.post("/tracking/sync", {"next": "/pipeline"})

    assert response.headers["location"] == "/pipeline"
    assert world.status(oid) is S.REPLIED
    assert client.get("/pipeline").status_code == 200


def test_sync_tracking_ignores_an_off_site_next(client: WebClient, world: World) -> None:
    response = client.post("/tracking/sync", {"next": "https://evil.example/"})
    assert response.headers["location"] == "/"


def test_pipeline_shows_when_replies_were_last_checked(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    _sent(client, world, ids["sig_test0001"])
    client.post("/tracking/sync")
    assert f"Last checked {NOW.strftime('%Y-%m-%d %H:%M UTC')}." in client.get("/pipeline").text


def test_sync_tracking_reports_errors_and_busy_lock(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    world.inbox.fail = True
    page = client.flash(client.post("/tracking/sync"))
    assert "1 error(s)" in page and "flash-error" in page
    assert world.store.acquire_owned_lock(RUN_LOCK, owner="other-holder", now=NOW, ttl_seconds=3600)
    assert "A run is in progress" in client.flash(client.post("/tracking/sync"))


def test_runs_page_lists_history(client: WebClient, world: World, ids: dict[str, int]) -> None:
    world.services.origination.run(RunTrigger.MANUAL, use_rocketreach=False)
    page = client.get("/runs").text
    assert 'id="run-1"' in page and 'id="run-2"' in page
    assert "cron" in page and "manual" in page


def test_runs_page_shows_run_errors(client: WebClient, world: World) -> None:
    world.catalog.fail = True
    world.services.origination.run(RunTrigger.CRON)
    page = client.get("/runs").text
    assert "Search catalogue sync failed" in page


# --------------------------------------------------------------------------- #
# Drafts (R13, R16)
# --------------------------------------------------------------------------- #


def test_drafts_page_renders_copy_blocks_with_exact_text(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    oid = ids["sig_test0001"]
    (item,) = world.services.outreach.list_by_status([S.DRAFTED])
    draft = item.draft
    assert draft is not None
    page = client.get("/drafts").text
    assert input_value(page, f"to-{oid}") == draft.to == "alex1@co1.example"
    assert input_value(page, f"subject-{oid}") == draft.subject
    assert textarea(page, f"body-{oid}") == draft.body
    assert textarea(page, f"all-{oid}") == draft.as_text()
    for target in ("to", "subject", "body", "all"):
        assert f'data-copy-target="{target}-{oid}"' in page
    assert "Copy all" in page
    # Founder context + email source badge.
    assert "Alex1 Testfounder" in page and "Example1 Limited" in page and "Manchester" in page
    assert "Building Example1 Limited" in page and "New Company" in page and "score 8" in page
    assert 'href="https://www.linkedin.com/in/alex-testfounder-1"' in page
    assert 'rel="noopener noreferrer"' in page and 'target="_blank"' in page
    assert "badge-evertrace" in page
    # Buttons.
    for action in ("sent", "close", "discard-email"):
        assert f'action="/outreach/{oid}/{action}"' in page


def test_drafts_body_keeps_line_breaks(client: WebClient, world: World, ids: dict[str, int]) -> None:
    page = client.get("/drafts").text
    body = textarea(page, f"body-{ids['sig_test0001']}")
    assert body.startswith("Hi Alex1,\n\n")


def test_every_copy_button_is_wired_to_a_plain_text_field(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    """R13: each copy button is a non-submitting button whose target exists on the page,
    and the page loads the local script that copies that field's plain-text value."""
    page = client.get("/drafts").text
    assert '<script src="/static/app.js" defer></script>' in page
    buttons = re.findall(r"<button([^>]*)data-copy-target=\"([^\"]+)\"", page)
    assert len(buttons) == 4  # To, Subject, Body, Copy all for the one draft
    for attrs, target in buttons:
        assert 'type="button"' in attrs  # never submits a form
        # The target is a form field (plain .value, no HTML), not a rendered element.
        assert re.search(rf'<(input|textarea) id="{re.escape(target)}"', page), target
    js = client.get("/static/app.js").text
    assert "data-copy-target" in js
    assert "var text = target.value;" in js  # plain text, line breaks preserved
    assert "navigator.clipboard.writeText(text)" in js
    assert 'execCommand("copy")' in js  # fallback outside secure contexts


def test_companies_house_link_rendered_only_for_the_official_site(client: WebClient, world: World) -> None:
    ch = "https://find-and-update.company-information.service.gov.uk/company/00000001"
    world.seed(
        make_signal(1, email="a@co1.example", company=CompanyRef(name="Good Ltd", registry_url=ch)),
        make_signal(
            2,
            email="b@co2.example",
            company=CompanyRef(name="Bad Ltd", registry_url="https://evil.example/x"),
        ),
    )
    page = client.get("/drafts").text
    assert f'href="{ch}"' in page
    assert "evil.example" not in page
    assert page.count(">Companies House</a>") == 1


def test_mark_sent_from_drafts(client: WebClient, world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0001"]
    response = client.post(f"/outreach/{oid}/sent", {"next": "/drafts"})
    assert response.headers["location"] == "/drafts"
    assert world.status(oid) is S.SENT
    assert "Marked as sent." in client.flash(response)
    events = world.store.list_status_events(oid)
    assert events[-1].to_status is S.SENT and events[-1].source.value == "manual"


def test_skip_closes_the_draft(client: WebClient, world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0001"]
    client.post(f"/outreach/{oid}/close", {"reason": "skipped", "next": "/drafts"})
    record = world.store.get_outreach(oid)
    assert record.status is S.CLOSED and record.close_reason is CloseReason.SKIPPED


def test_wrong_email_moves_back_to_needs_email(client: WebClient, world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0001"]
    client.post(f"/outreach/{oid}/discard-email", {"next": "/drafts"})
    record = world.store.get_outreach(oid)
    assert record.status is S.NEEDS_EMAIL and record.email is None


# --------------------------------------------------------------------------- #
# Needs email (R11)
# --------------------------------------------------------------------------- #


def test_needs_email_page_and_manual_email_creates_draft(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    oid = ids["sig_test0002"]
    page = client.get("/needs-email").text
    assert f'action="/outreach/{oid}/email"' in page and "Alex2 Testfounder" in page
    response = client.post(f"/outreach/{oid}/email", {"email": " Alex2@Co2.example ", "next": "/needs-email"})
    assert "Email saved and draft created" in client.flash(response)
    record = world.store.get_outreach(oid)
    assert record.status is S.DRAFTED and record.email_source is EmailSource.MANUAL
    assert record.email == "Alex2@co2.example"
    assert f'id="all-{oid}"' in client.get("/drafts").text


def test_invalid_manual_email_is_rejected(client: WebClient, world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0002"]
    response = client.post(f"/outreach/{oid}/email", {"email": "not-an-email", "next": "/needs-email"})
    page = client.flash(response)
    assert "That email address is not valid" in page and "flash-error" in page
    assert world.status(oid) is S.NEEDS_EMAIL


def test_header_injection_in_manual_email_is_rejected(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    oid = ids["sig_test0002"]
    client.post(f"/outreach/{oid}/email", {"email": "a@co.example\r\nBcc: x@evil.example"})
    assert world.status(oid) is S.NEEDS_EMAIL


def test_skip_from_needs_email(client: WebClient, world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0002"]
    client.post(f"/outreach/{oid}/close", {"reason": "skipped", "next": "/needs-email"})
    assert world.status(oid) is S.CLOSED


# --------------------------------------------------------------------------- #
# Excluded (R7)
# --------------------------------------------------------------------------- #


def test_excluded_page_shows_reason_and_detail(client: WebClient, ids: dict[str, int]) -> None:
    page = client.get("/excluded").text
    assert "Alex3 Testfounder" in page and "Alex4 Testfounder" in page
    assert "location" in page and "London" in page
    assert f'action="/outreach/{ids["sig_test0003"]}/draft-anyway"' in page


def test_draft_anyway_with_known_email_drafts(client: WebClient, world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0003"]
    page = client.flash(client.post(f"/outreach/{oid}/draft-anyway", {"next": "/excluded"}))
    assert "draft created" in page
    assert world.status(oid) is S.DRAFTED


def test_draft_anyway_without_email_needs_email(client: WebClient, world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0004"]
    page = client.flash(client.post(f"/outreach/{oid}/draft-anyway", {"next": "/excluded"}))
    assert "no email known yet" in page
    assert world.status(oid) is S.NEEDS_EMAIL


def test_draft_anyway_on_a_non_excluded_founder_flashes_the_service_error(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    oid = ids["sig_test0001"]
    page = client.flash(client.post(f"/outreach/{oid}/draft-anyway", {"next": "/excluded"}))
    assert "Only excluded founders can be drafted anyway" in page
    assert world.status(oid) is S.DRAFTED


# --------------------------------------------------------------------------- #
# Pipeline + detail (R16, R17)
# --------------------------------------------------------------------------- #


def _sent(client: WebClient, world: World, oid: int) -> None:
    client.post(f"/outreach/{oid}/sent")
    assert world.status(oid) is S.SENT


def test_pipeline_sections_days_waiting_and_follow_up_flag(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    oid = ids["sig_test0001"]
    _sent(client, world, oid)
    world.clock.advance(days=15)
    client.login()  # 15 days later the 12-hour session has expired
    page = client.get("/pipeline").text
    for status in ("sent", "replied", "deck_received"):
        assert f'id="column-{status}"' in page
    assert f'<tr id="outreach-{oid}" class="follow-up">' in page
    assert "Follow up" in page and "<td>15" in page
    assert NOW.strftime("%Y-%m-%d %H:%M UTC") in page  # sent timestamp
    # Replies are detected automatically: the only per-row choices are deck / declined.
    assert f'action="/outreach/{oid}/replied"' not in page
    assert f'action="/outreach/{oid}/deck-received"' in page
    assert f'action="/outreach/{oid}/close"' in page
    assert '<input type="hidden" name="reason" value="declined">' in page
    assert "<option value=" not in page
    assert page.index('id="column-replied"') < page.index('id="column-sent"')
    overview = client.get("/").text
    assert "Follow-ups due (1)" in overview and f'href="/outreach/{oid}"' in overview


def test_pipeline_buttons_follow_the_state_machine(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    oid = ids["sig_test0001"]
    _sent(client, world, oid)
    client.post(f"/outreach/{oid}/replied", {"next": "/pipeline"})
    assert world.status(oid) is S.REPLIED
    page = client.get("/pipeline").text
    assert f'action="/outreach/{oid}/replied"' not in page
    client.post(f"/outreach/{oid}/deck-received", {"next": "/pipeline"})
    assert world.status(oid) is S.DECK_RECEIVED
    page = client.get("/pipeline").text
    assert f'action="/outreach/{oid}/deck-received"' not in page
    assert '<option value="declined">' in page  # deck-received rows keep the full close menu
    response = client.post(f"/outreach/{oid}/close", {"reason": "declined", "next": "/pipeline"})
    assert "Closed (declined)." in client.flash(response)
    record = world.store.get_outreach(oid)
    assert record.status is S.CLOSED and record.close_reason is CloseReason.DECLINED
    targets = [e.to_status for e in world.store.list_status_events(oid)]
    assert targets[-4:] == [S.SENT, S.REPLIED, S.DECK_RECEIVED, S.CLOSED]


def test_invalid_transition_is_flashed_not_applied(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    oid = ids["sig_test0002"]  # NEEDS_EMAIL
    page = client.flash(client.post(f"/outreach/{oid}/replied", {"next": "/pipeline"}))
    assert "Cannot move from needs email to replied" in page
    assert world.status(oid) is S.NEEDS_EMAIL


def test_unknown_close_reason_is_rejected(client: WebClient, world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0001"]
    response = client.post(f"/outreach/{oid}/close", {"reason": "because"})
    assert response.status_code == 400
    assert "because" not in response.text
    assert world.status(oid) is S.DRAFTED


def test_action_on_missing_outreach_flashes_not_found(client: WebClient) -> None:
    page = client.flash(client.post("/outreach/999/sent", {"next": "/pipeline"}))
    assert "no longer exists" in page


def test_detail_page_shows_timeline_draft_and_actions(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    oid = ids["sig_test0001"]
    page = client.get(f"/outreach/{oid}").text
    assert "Timeline" in page and "discovered" in page and "drafted" in page
    assert textarea(page, f"all-{oid}").startswith("To: alex1@co1.example\nSubject: Funding Enquiry\n\n")
    assert f'action="/outreach/{oid}/forget"' in page and 'name="confirm"' in page
    _sent(client, world, oid)
    page = client.get(f"/outreach/{oid}").text
    assert "(manual)" in page and f'action="/outreach/{oid}/deck-received"' in page


def test_detail_page_for_excluded_and_needs_email(client: WebClient, ids: dict[str, int]) -> None:
    page = client.get(f"/outreach/{ids['sig_test0003']}").text
    assert "Draft anyway" in page and "Excluded" in page
    page = client.get(f"/outreach/{ids['sig_test0002']}").text
    assert f'action="/outreach/{ids["sig_test0002"]}/email"' in page


def test_detail_shows_rocketreach_lookup(client: WebClient, world: World) -> None:
    from app.domain.models import EmailLookupResult, LookupOutcome

    world.finder.results["alex-testfounder-5"] = EmailLookupResult(
        outcome=LookupOutcome.REJECTED, reason="Slug mismatch"
    )
    world.enable_search()
    world.signals.by_filters[world.catalog.searches[0].filters] = [make_signal(5)]
    world.services.origination.run(RunTrigger.CRON)
    (view,) = world.store.list_outreach()
    page = client.get(f"/outreach/{view.outreach.id}").text
    assert "RocketReach" in page and "rejected" in page and "Slug mismatch" in page


def test_detail_unknown_id_is_404(client: WebClient) -> None:
    response = client.get("/outreach/424242")
    assert response.status_code == 404
    assert "That outreach record no longer exists" in response.text
    assert client.get("/outreach/abc").status_code == 404
    assert client.get("/outreach/0").status_code == 400


def test_forget_requires_the_confirmation_box(client: WebClient, world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0001"]
    response = client.post(f"/outreach/{oid}/forget")
    assert response.headers["location"] == f"/outreach/{oid}"
    assert "Tick the confirmation box" in client.flash(response)
    assert world.status(oid) is S.DRAFTED


def test_forget_founder_hard_deletes(client: WebClient, world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0001"]
    response = client.post(f"/outreach/{oid}/forget", {"confirm": "yes"})
    assert response.headers["location"] == "/"
    assert "Founder forgotten" in client.flash(response)
    assert client.get(f"/outreach/{oid}").status_code == 404
    assert all(v.founder.evertrace_signal_id != "sig_test0001" for v in world.store.list_outreach())
    page = client.flash(client.post(f"/outreach/{oid}/forget", {"confirm": "yes"}))
    assert "no longer exists" in page


# --------------------------------------------------------------------------- #
# Searches (R18, R19)
# --------------------------------------------------------------------------- #

NORTH = SavedSearch(
    external_id="sch_north",
    title="North founders",
    filters=(SearchFilter(key="country", values=("United Kingdom",)),),
)


def test_sync_from_evertrace_lists_searches_disabled(client: WebClient, world: World) -> None:
    world.catalog.searches.append(NORTH)
    response = client.post("/searches/sync")
    assert "Synced from Evertrace: 1 search(es)" in client.flash(response)
    page = client.get("/searches").text
    assert "North founders" in page and "sch_north" in page and ">disabled<" in page


def test_sync_failure_is_flashed(client: WebClient, world: World) -> None:
    world.catalog.fail = True
    page = client.flash(client.post("/searches/sync"))
    assert "Could not sync searches from Evertrace" in page and "flash-error" in page


def test_update_search_settings(client: WebClient, world: World) -> None:
    world.catalog.searches.append(NORTH)
    (config,) = world.services.searches.sync_catalog()
    response = client.post(
        f"/searches/{config.id}",
        {"enabled": "true", "location_policy": "london_allowed", "lookback_days": "60"},
    )
    assert "(enabled)" in client.flash(response)
    saved = world.services.searches.get_config(config.id or 0)
    assert (
        saved.enabled and saved.location_policy is LocationPolicy.LONDON_ALLOWED and saved.lookback_days == 60
    )
    # An unticked checkbox is not submitted: the search is disabled.
    client.post(f"/searches/{config.id}", {"location_policy": "uk_outside_london", "lookback_days": "30"})
    assert not world.services.searches.get_config(config.id or 0).enabled


def test_update_search_rejects_bad_values(client: WebClient, world: World) -> None:
    world.catalog.searches.append(NORTH)
    (config,) = world.services.searches.sync_catalog()
    for data in (
        {"location_policy": "everywhere", "lookback_days": "30"},
        {"location_policy": "uk_outside_london", "lookback_days": "9999"},
        {"location_policy": "uk_outside_london", "lookback_days": "abc"},
    ):
        assert client.post(f"/searches/{config.id}", data).status_code == 400
    assert world.services.searches.get_config(config.id or 0).lookback_days == 30
    page = client.flash(
        client.post("/searches/999", {"location_policy": "uk_outside_london", "lookback_days": "30"})
    )
    assert "That search no longer exists" in page


def test_run_this_search_now(client: WebClient, world: World) -> None:
    world.catalog.searches.append(NORTH)
    (config,) = world.services.searches.sync_catalog()  # disabled, but an explicit run still runs it
    world.signals.by_filters[NORTH.filters] = [make_signal(7, email="alex7@co7.example")]
    response = client.post(f"/searches/{config.id}/run")
    assert "Run started for 'North founders'" in client.flash(response)
    latest = world.store.latest_run()
    assert latest is not None and latest.trigger is RunTrigger.MANUAL and latest.counts.drafted == 1
    assert "no longer exists" in client.flash(client.post("/searches/999/run"))


def test_new_search_form_is_built_from_the_vocabulary(client: WebClient) -> None:
    page = client.get("/searches/new").text
    for option in ("New Company", "Stealth Position", "BatteryTech", "Application Software", "woman"):
        assert f'<option value="{option}"' in page
    assert '<option value="United Kingdom" selected>' in page
    assert 'name="city_include"' in page and 'name="city_exclude"' in page and 'name="min_score"' in page


def test_new_search_form_when_vocabulary_is_unavailable(
    client: WebClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> None:
        raise IntegrationError("evertrace", "unavailable")

    monkeypatch.setattr(world.catalog, "vocabulary", fail)
    response = client.get("/searches/new")
    assert response.status_code == 502
    assert "Could not load Evertrace filter options" in response.text
    response = client.post("/searches/new", {"title": "X", "country": ["United Kingdom"]})
    assert response.status_code == 502 and world.catalog.created == []


def test_create_search_on_evertrace_with_backfill(client: WebClient, world: World) -> None:
    world.signals.by_filters = {}
    response = client.post(
        "/searches/new",
        {
            "title": "  Battery founders  ",
            "status": ["new company"],
            "industry": ["BatteryTech"],
            "country": ["United Kingdom"],
            "gender": ["woman"],
            "min_score": "7",
            "city_include": "Leeds, Manchester",
            "city_exclude": "London",
            "location_policy": "london_allowed",
            "lookback_days": "45",
            "enabled": "true",
            "backfill": "true",
        },
    )
    assert response.status_code == 303 and response.headers["location"] == "/searches"
    (spec,) = world.catalog.created
    assert spec.title == "Battery founders"
    filters = {f.key: f.values for f in spec.filters}
    assert filters["status"] == ("New Company",)
    assert filters["industry"] == ("BatteryTech",)
    assert filters["country"] == ("United Kingdom",)
    assert filters["gender"] == ("woman",)
    assert filters["score"] == ("7",)
    assert filters["city"] == ("Leeds", "Manchester", "!London")
    config = world.services.searches.find_by_external_id("sch_new1")
    assert config is not None and config.enabled
    assert config.location_policy is LocationPolicy.LONDON_ALLOWED and config.lookback_days == 45
    latest = world.store.latest_run()
    assert latest is not None and latest.trigger is RunTrigger.MANUAL  # the backfill ran
    assert "Backfill of the last 45 days started" in client.flash(response)


def test_create_search_without_backfill_does_not_run(client: WebClient, world: World) -> None:
    response = client.post(
        "/searches/new", {"title": "Quiet", "country": ["United Kingdom"], "lookback_days": "30"}
    )
    assert response.status_code == 303
    assert len(world.catalog.created) == 1 and world.store.latest_run() is None
    config = world.services.searches.find_by_external_id("sch_new1")
    assert config is not None and not config.enabled


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"title": "", "country": ["United Kingdom"]}, "Title must be 1-50 characters"),
        ({"title": "x" * 51, "country": ["United Kingdom"]}, "Title must be 1-50 characters"),
        ({"title": "No filters", "country": []}, "Add at least one filter"),
        ({"title": "Bad", "industry": ["Underwater Basket Weaving"]}, "is not a valid industry"),
        ({"title": "Bad score", "min_score": "11"}, "Minimum score must be a whole number from 1 to 10"),
        (
            {"title": "Bad lookback", "country": ["United Kingdom"], "lookback_days": "0"},
            "Lookback must be 1-730",
        ),
        (
            {"title": "Bad policy", "country": ["United Kingdom"], "location_policy": "mars"},
            "Location policy must",
        ),
        ({"title": "City", "city_include": "Leeds", "city_exclude": "leeds"}, "both included and excluded"),
        ({"title": "Many", "industry": ["BatteryTech"] * 61}, "too many values"),
        ({"title": "Long", "industry": ["x" * 101]}, "a value is too long"),
    ],
)
def test_create_search_validation_errors_are_shown(
    client: WebClient, world: World, data: dict[str, object], message: str
) -> None:
    payload: dict[str, object] = {"lookback_days": "30", "location_policy": "uk_outside_london", **data}
    response = client.post("/searches/new", payload)
    assert response.status_code == 400
    assert message in html.unescape(response.text)
    assert world.catalog.created == []
    assert 'action="/searches/new"' in response.text  # the form is re-rendered


def test_create_search_keeps_the_users_input_on_error(client: WebClient) -> None:
    response = client.post(
        "/searches/new",
        {"title": "", "industry": ["BatteryTech"], "city_include": "Leeds", "lookback_days": "30"},
    )
    assert '<option value="BatteryTech" selected>' in response.text
    assert 'value="Leeds"' in response.text


def test_create_search_evertrace_failure(
    client: WebClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(spec: object) -> None:
        raise IntegrationError("evertrace", "create failed")

    monkeypatch.setattr(world.catalog, "create_search", fail)
    response = client.post(
        "/searches/new", {"title": "T", "country": ["United Kingdom"], "lookback_days": "30"}
    )
    assert response.status_code == 502
    assert "Evertrace did not create the search" in response.text


# --------------------------------------------------------------------------- #
# Template (R12, R14)
# --------------------------------------------------------------------------- #


def test_template_page_shows_version_fields_and_preview_area(client: WebClient) -> None:
    page = client.get("/template").text
    assert "current version 1" in page
    for name in ("first_name", "last_name", "full_name", "company_name"):
        assert "<code>{" + name + "}</code>" in page
    assert 'id="preview-text"' in page and 'data-preview-url="/template/preview"' in page
    assert 'action="/template/rerender"' in page
    assert textarea(page, "body").startswith("Hi {first_name},")


def test_save_template_rerenders_open_drafts(client: WebClient, world: World, ids: dict[str, int]) -> None:
    response = client.post(
        "/template",
        {
            "subject": "Hello {company_name}",
            "body": "Dear {first_name},\r\nAre you raising?",
            "rerender": "true",
        },
    )
    assert "Template saved as version 2; 1 open draft(s) re-rendered." in client.flash(response)
    record = world.store.get_outreach(ids["sig_test0001"])
    assert record.draft_subject == "Hello Example1 Limited"
    assert record.draft_body == "Dear Alex1,\nAre you raising?"
    assert record.template_version == 2


def test_save_template_without_rerender_then_rerender_button(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    oid = ids["sig_test0001"]
    response = client.post("/template", {"subject": "New subject", "body": "Hi {first_name}"})
    assert "Open drafts keep their old wording" in client.flash(response)
    assert world.store.get_outreach(oid).draft_subject == "Funding Enquiry"
    response = client.post("/template/rerender")
    assert "1 open draft(s) re-rendered" in client.flash(response)
    assert world.store.get_outreach(oid).draft_subject == "New subject"


def test_invalid_template_is_rejected_with_problems(client: WebClient, world: World) -> None:
    response = client.post("/template", {"subject": "Hi {nickname}", "body": "", "rerender": "true"})
    assert response.status_code == 400
    text = html.unescape(response.text)
    assert "Unknown merge field {nickname}" in text and "Body is empty" in text
    assert input_value(response.text, "subject") == "Hi {nickname}"  # user input kept
    assert world.services.templates.current().version == 1


def test_template_preview_endpoint(client: WebClient) -> None:
    client.get("/template")
    headers = {"X-CSRF-Token": client.csrf, "Accept": "application/json"}
    response = client.client.post(
        "/template/preview",
        data={"subject": "For {company_name}", "body": "Hi {first_name}"},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data == {
        "ok": True,
        "to": "sam@sample-robotics.example",
        "subject": "For Sample Robotics",
        "body": "Hi Sam",
        "text": "To: sam@sample-robotics.example\nSubject: For Sample Robotics\n\nHi Sam",
    }
    bad = client.client.post("/template/preview", data={"subject": "x", "body": "{oops}"}, headers=headers)
    assert bad.status_code == 422 and bad.json()["ok"] is False
    assert any("{oops}" in p for p in bad.json()["problems"])
