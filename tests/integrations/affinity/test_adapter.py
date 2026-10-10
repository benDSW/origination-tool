"""AffinityAdapter: list snapshot mapping and email-interaction classification.

All payloads are synthetic and shaped like the live v1/v2 responses.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx

from app.domain.matching import KnownIndex, find_crm_match
from app.domain.models import CompanyRef, InteractionDirection, KnownEntity
from app.domain.ports import CrmGateway, IntegrationError
from app.integrations.affinity import AffinityAdapter, AffinityClient
from tests.fakes import utc
from tests.integrations.affinity.conftest import (
    APPLICANT_LIST,
    DSW_DOMAINS,
    FAKE_KEY,
    NOW,
    ORIGINATION_LIST,
    fixed_clock,
)

V2 = "https://api.affinity.co/v2"
V1 = "https://api.affinity.co"
FOUNDER = "jo.synthetic@founderco.example"


# --------------------------------------------------------------------------- #
# Payload builders (synthetic)
# --------------------------------------------------------------------------- #


def companies_field(*companies: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "companies",
        "type": "list",
        "name": "Organizations",
        "value": {"type": "company-multi", "totalCount": len(companies), "data": list(companies)},
    }


def persons_field(*persons: dict[str, Any]) -> dict[str, Any]:
    return {"id": "persons", "name": "People", "value": {"type": "person-multi", "data": list(persons)}}


def entry(entry_id: int, name: str | None, *fields: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "id": entry_id,
        "listId": ORIGINATION_LIST,
        "type": "opportunity",
        "createdAt": "2026-01-01T00:00:00Z",
        "entity": {
            "id": entry_id + 10_000,
            "listId": ORIGINATION_LIST,
            "name": name,
            "fields": list(fields),
            "isRestricted": False,
            "isRedacted": False,
        },
        **extra,
    }


def page(data: list[Any], next_url: str | None = None) -> dict[str, Any]:
    return {"data": data, "pagination": {"prevUrl": None, "nextUrl": next_url}}


def mock_list(router: respx.MockRouter, list_id: int, name: Any, pages: list[dict[str, Any]]) -> None:
    router.get(f"{V2}/lists/{list_id}").respond(json={"id": list_id, "name": name, "type": "opportunity"})
    router.get(f"{V2}/lists/{list_id}/list-entries").mock(
        side_effect=[httpx.Response(200, json=p) for p in pages]
    )


def person(pid: Any, primary: str | None, emails: list[Any] | None = None) -> dict[str, Any]:
    return {
        "id": pid,
        "type": 0,
        "first_name": "Jo",
        "last_name": "Synthetic",
        "primary_email": primary,
        "emails": emails if emails is not None else ([primary] if primary else []),
    }


def email(date: Any, sender: dict[str, Any] | None, subject: Any = "Funding Enquiry", direction: int = 0):
    return {
        "date": date,
        "id": 1,
        "subject": subject,
        "type": 3,
        "from": sender,
        "to": [],
        "cc": [],
        "direction": direction,
    }


DSW_SENDER = {"id": 7, "first_name": "Sam", "last_name": "Staff", "primary_email": "sam@dsw.example"}
FOUNDER_SENDER = {"id": 8, "first_name": "Jo", "last_name": "Synthetic", "primary_email": FOUNDER}
THIRD_PARTY = {"id": 9, "first_name": "Cc", "last_name": "Person", "primary_email": "cc@otherfund.example"}


def mock_person(router: respx.MockRouter, *people: dict[str, Any]) -> respx.Route:
    return router.get(f"{V1}/persons").respond(json={"persons": list(people), "next_page_token": None})


def mock_interactions(router: respx.MockRouter, *pages: dict[str, Any]) -> respx.Route:
    return router.get(f"{V1}/interactions").mock(side_effect=[httpx.Response(200, json=p) for p in pages])


# --------------------------------------------------------------------------- #
# Port conformance / construction
# --------------------------------------------------------------------------- #


def test_adapter_satisfies_crm_gateway_port(adapter: AffinityAdapter) -> None:
    assert isinstance(adapter, CrmGateway)


def test_requires_a_dsw_domain(client: AffinityClient) -> None:
    with pytest.raises(ValueError):
        AffinityAdapter(client, list_ids=[1], dsw_email_domains=["  "])


def test_from_api_key_builds_a_working_adapter(router: respx.MockRouter) -> None:
    route = router.get(f"{V2}/lists/5").respond(json={"id": 5, "name": "Solo"})
    router.get(f"{V2}/lists/5/list-entries").respond(json=page([]))
    adapter = AffinityAdapter.from_api_key(FAKE_KEY, list_ids=[5, 5], dsw_email_domains=["@DSW.example"])
    try:
        assert adapter.known_entities().entities == ()
    finally:
        adapter.close()
    assert route.call_count == 1  # duplicate list ids are fetched once


# --------------------------------------------------------------------------- #
# known_entities
# --------------------------------------------------------------------------- #


def test_known_entities_maps_both_lists_with_pagination(router: respx.MockRouter, adapter: AffinityAdapter):
    next_url = f"{V2}/lists/{ORIGINATION_LIST}/list-entries?cursor=c2"
    mock_list(
        router,
        ORIGINATION_LIST,
        "Origination Opportunities",
        [
            page(
                [
                    entry(
                        1,
                        "Widgetry - Seed",
                        companies_field({"id": 11, "name": "Widgetry Ltd", "domain": "widgetry.example"}),
                        persons_field(
                            {"id": 21, "firstName": "Jo", "lastName": "Bloggs", "primaryEmailAddress": None}
                        ),
                    )
                ],
                next_url,
            ),
            page([entry(2, "Gizmo Labs", companies_field({"id": 12, "name": "Gizmo Labs", "domain": None}))]),
        ],
    )
    mock_list(
        router,
        APPLICANT_LIST,
        "Applicant Opportunities",
        [page([entry(3, "Sprocket", persons_field({"id": 22, "firstName": "Ali", "lastName": "Example"}))])],
    )

    known = adapter.known_entities()

    assert known.entities == (
        KnownEntity(
            list_name="Origination Opportunities",
            opportunity_name="Widgetry - Seed",
            company_names=("Widgetry Ltd",),
            company_domains=("widgetry.example",),
            person_names=("Jo Bloggs",),
        ),
        KnownEntity(
            list_name="Origination Opportunities",
            opportunity_name="Gizmo Labs",
            company_names=("Gizmo Labs",),
        ),
        KnownEntity(
            list_name="Applicant Opportunities", opportunity_name="Sprocket", person_names=("Ali Example",)
        ),
    )
    # The snapshot plugs straight into domain matching.
    index = KnownIndex.build(known)
    match = find_crm_match(index, CompanyRef(name="Widgetry Limited"), "Nobody Here")
    assert match is not None and match.list_name == "Origination Opportunities"


def test_list_entries_request_asks_for_company_and_person_fields(router: respx.MockRouter, client):
    router.get(f"{V2}/lists/1").respond(json={"id": 1, "name": "L"})
    route = router.get(f"{V2}/lists/1/list-entries").respond(json=page([]))
    AffinityAdapter(client, list_ids=[1], dsw_email_domains=DSW_DOMAINS).known_entities()
    params = route.calls.last.request.url.params
    assert params.get_list("fieldIds") == ["companies", "persons"]
    assert params["limit"] == "100"


@pytest.mark.parametrize("name", [None, "", "   ", 42])
def test_list_name_falls_back_to_id(router: respx.MockRouter, client: AffinityClient, name: Any) -> None:
    mock_list(router, 77, name, [page([entry(1, "Thing Co")])])
    known = AffinityAdapter(client, list_ids=[77], dsw_email_domains=DSW_DOMAINS).known_entities()
    assert known.entities[0].list_name == "List 77"


def test_known_entities_is_defensive_about_missing_fields(router: respx.MockRouter, client) -> None:
    entries: list[Any] = [
        "not-a-dict",
        {"id": 1, "type": "opportunity"},  # no entity
        {"id": 2, "type": "opportunity", "entity": None},
        entry(3, None),  # nothing to match on -> skipped (e.g. redacted)
        {"id": 4, "type": "opportunity", "entity": {"name": "Bare Opp", "fields": None}},
        entry(
            5,
            None,  # opportunity name falls back to first company name
            companies_field(
                {"id": 1, "name": "  Fallback Co  "}, {"id": 2, "name": None, "domain": "x.example"}
            ),
            {"id": "persons", "value": None},
            {"id": "other", "value": {"type": "text", "data": "ignored"}},
            "junk-field",
        ),
        entry(6, None, persons_field({"id": 1, "firstName": "Solo"}, {"id": 2}, {"lastName": "Only"})),
        entry(7, "Weird Value", {"id": "companies", "value": {"type": "company-multi", "data": None}}),
        entry(8, "Single", {"id": "x", "value": {"type": "company", "data": {"name": "One Co"}}}),
        entry(
            9,
            "Dupes",
            companies_field(
                {"name": "Dup Co", "domain": "dup.example"}, {"name": "Dup Co", "domain": "dup.example"}
            ),
        ),
    ]
    mock_list(router, 1, "Watched", [{"data": entries}])  # no pagination block at all
    known = AffinityAdapter(client, list_ids=[1], dsw_email_domains=DSW_DOMAINS).known_entities()
    by_name = {e.opportunity_name: e for e in known.entities}
    assert set(by_name) == {"Bare Opp", "Fallback Co", "Entry 6", "Weird Value", "Single", "Dupes"}
    assert by_name["Fallback Co"].company_names == ("Fallback Co",)
    assert by_name["Fallback Co"].company_domains == ("x.example",)
    assert by_name["Entry 6"].person_names == ("Solo", "Only")
    assert by_name["Weird Value"].company_names == ()
    assert by_name["Single"].company_names == ("One Co",)
    assert by_name["Dupes"].company_names == ("Dup Co",)
    assert by_name["Dupes"].company_domains == ("dup.example",)


def test_known_entities_tolerates_company_and_person_lists(router: respx.MockRouter, client) -> None:
    data = [
        {"id": 1, "type": "company", "entity": {"id": 5, "name": "Listed Co", "domain": "listed.example"}},
        {"id": 2, "type": "person", "entity": {"id": 6, "firstName": "Pat", "lastName": "Sample"}},
    ]
    mock_list(router, 1, "Mixed", [page(data)])
    known = AffinityAdapter(client, list_ids=[1], dsw_email_domains=DSW_DOMAINS).known_entities()
    assert known.entities[0].company_names == ("Listed Co",)
    assert known.entities[0].company_domains == ("listed.example",)
    assert known.entities[1].person_names == ("Pat Sample",)
    assert known.entities[1].opportunity_name == "Entry 2"


def test_known_entities_propagates_auth_failure(router: respx.MockRouter, adapter: AffinityAdapter) -> None:
    router.get(f"{V2}/lists/{ORIGINATION_LIST}").respond(401)
    with pytest.raises(IntegrationError) as exc:
        adapter.known_entities()
    assert exc.value.retryable is False
    assert FAKE_KEY not in str(exc.value)


def test_known_entities_refuses_foreign_next_url(router: respx.MockRouter, adapter: AffinityAdapter) -> None:
    mock_list(router, ORIGINATION_LIST, "O", [page([entry(1, "A")], "https://evil.example/next")])
    with pytest.raises(IntegrationError, match="untrusted"):
        adapter.known_entities()


# --------------------------------------------------------------------------- #
# Person lookup
# --------------------------------------------------------------------------- #


def test_invalid_email_makes_no_request(router: respx.MockRouter, adapter: AffinityAdapter) -> None:
    assert adapter.email_interactions("not an email", utc(2026, 9, 1)) == []
    assert adapter.email_interactions("", utc(2026, 9, 1)) == []
    assert not router.calls


def test_unknown_person_returns_empty(router: respx.MockRouter, adapter: AffinityAdapter) -> None:
    route = mock_person(router)
    interactions = router.get(f"{V1}/interactions").respond(json={"emails": []})
    assert adapter.email_interactions(FOUNDER, utc(2026, 9, 1)) == []
    assert route.calls.last.request.url.params["term"] == FOUNDER
    assert route.calls.last.request.url.params["page_size"] == "10"
    assert not interactions.called


def test_fuzzy_person_results_are_not_accepted(router: respx.MockRouter, adapter: AffinityAdapter) -> None:
    mock_person(
        router,
        person(1, "jo.synthetic@founderco.example.org"),
        person(2, "other@founderco.example", ["jo.synthetic@founderco.examplex"]),
        person(3, None, None),
    )
    interactions = router.get(f"{V1}/interactions").respond(json={"emails": []})
    assert adapter.email_interactions(FOUNDER, utc(2026, 9, 1)) == []
    assert not interactions.called


def test_person_matched_case_insensitively_via_secondary_emails(router: respx.MockRouter, adapter) -> None:
    mock_person(
        router,
        person("bad-id", FOUNDER),  # non-int id is ignored
        person(True, FOUNDER),  # bool is not a valid id either
        person(42, "jo@personal.example", ["jo@personal.example", "JO.Synthetic@FounderCo.example", None]),
    )
    route = mock_interactions(router, {"emails": [], "next_page_token": None})
    adapter.email_interactions(FOUNDER.upper(), utc(2026, 9, 1))
    assert route.calls.last.request.url.params["person_id"] == "42"


# --------------------------------------------------------------------------- #
# Interactions: window, pagination, direction
# --------------------------------------------------------------------------- #


def test_interaction_window_params_and_one_year_clamp(router: respx.MockRouter, adapter) -> None:
    mock_person(router, person(42, FOUNDER))
    route = mock_interactions(router, {"emails": []}, {"emails": []})

    adapter.email_interactions(FOUNDER, NOW - timedelta(days=800))
    params = route.calls[0].request.url.params
    assert params["type"] == "3"
    assert params["page_size"] == "100"
    assert params["end_time"] == "2026-10-09T12:00:00Z"
    assert params["start_time"] == "2025-10-10T12:00:00Z"  # now - 364 days

    adapter.email_interactions(FOUNDER, utc(2026, 9, 1, 8, 30))
    assert route.calls[1].request.url.params["start_time"] == "2026-09-01T08:30:00Z"


def test_naive_since_is_treated_as_utc(router: respx.MockRouter, adapter: AffinityAdapter) -> None:
    mock_person(router, person(42, FOUNDER))
    route = mock_interactions(router, {"emails": []})
    adapter.email_interactions(FOUNDER, utc(2026, 9, 1).replace(tzinfo=None))
    assert route.calls.last.request.url.params["start_time"] == "2026-09-01T00:00:00Z"


def test_since_in_the_future_skips_interaction_call(router: respx.MockRouter, adapter) -> None:
    mock_person(router, person(42, FOUNDER))
    route = router.get(f"{V1}/interactions").respond(json={"emails": []})
    assert adapter.email_interactions(FOUNDER, NOW + timedelta(hours=1)) == []
    assert not route.called


def test_interactions_are_paginated_classified_and_sorted(router: respx.MockRouter, adapter) -> None:
    mock_person(router, person(42, FOUNDER))
    route = mock_interactions(
        router,
        {
            "emails": [
                # Reply listed first to prove output is sorted; integer direction deliberately wrong.
                email("2026-09-05T09:00:00Z", FOUNDER_SENDER, "Re: Funding Enquiry", direction=0),
                email("2026-09-02T10:00:00.000-07:00", DSW_SENDER, direction=1),
            ],
            "next_page_token": "p2",
        },
        {
            "emails": [
                email("2026-09-06T09:00:00Z", THIRD_PARTY),  # third party -> skipped
                email(
                    "2026-09-07T09:00:00",  # naive -> UTC
                    {"id": 10, "primary_email": None, "emails": ["Partner@DSW.example"]},
                    subject=None,
                ),
                email("2026-09-08T09:00:00Z", FOUNDER_SENDER, subject="x" * 400),
            ],
            "next_page_token": None,
        },
    )

    result = adapter.email_interactions(FOUNDER, utc(2026, 9, 1))

    assert [(i.direction, i.occurred_at) for i in result] == [
        (InteractionDirection.OUTBOUND, utc(2026, 9, 2, 17, 0)),
        (InteractionDirection.INBOUND, utc(2026, 9, 5, 9, 0)),
        (InteractionDirection.OUTBOUND, utc(2026, 9, 7, 9, 0)),
        (InteractionDirection.INBOUND, utc(2026, 9, 8, 9, 0)),
    ]
    assert result[0].subject == "Funding Enquiry"
    assert result[2].subject is None
    assert len(result[3].subject or "") == 300
    assert route.calls[1].request.url.params["page_token"] == "p2"
    assert route.calls[1].request.url.params["person_id"] == "42"


def test_dsw_sender_wins_even_if_founder_address_also_listed(router: respx.MockRouter, adapter) -> None:
    mock_person(router, person(42, FOUNDER))
    sender = {"primary_email": FOUNDER, "emails": [FOUNDER, "jo@dsw.example"]}
    mock_interactions(router, {"emails": [email("2026-09-02T10:00:00Z", sender)]})
    [only] = adapter.email_interactions(FOUNDER, utc(2026, 9, 1))
    assert only.direction is InteractionDirection.OUTBOUND


def test_bad_or_out_of_window_interactions_are_skipped(router: respx.MockRouter, adapter) -> None:
    mock_person(router, person(42, FOUNDER))
    mock_interactions(
        router,
        {
            "emails": [
                email(None, DSW_SENDER),
                email("yesterday", DSW_SENDER),
                email(12345, DSW_SENDER),
                email("2026-08-01T00:00:00Z", DSW_SENDER),  # before since
                email("2026-09-02T10:00:00Z", None),  # no sender
                email("2026-09-02T10:00:00Z", {"primary_email": "no-at-sign"}),
                email("2026-09-03T10:00:00Z", DSW_SENDER),
            ]
        },
    )
    result = adapter.email_interactions(FOUNDER, utc(2026, 9, 1))
    assert [i.occurred_at for i in result] == [utc(2026, 9, 3, 10, 0)]


def test_interactions_errors_propagate(router: respx.MockRouter, adapter, sleeps: list[float]) -> None:
    mock_person(router, person(42, FOUNDER))
    router.get(f"{V1}/interactions").respond(500)
    with pytest.raises(IntegrationError) as exc:
        adapter.email_interactions(FOUNDER, utc(2026, 9, 1))
    assert exc.value.retryable is True
    assert FOUNDER not in str(exc.value)
    assert len(sleeps) == 3


def test_clock_is_used_for_end_time(router: respx.MockRouter, client: AffinityClient) -> None:
    adapter = AffinityAdapter(
        client, list_ids=[], dsw_email_domains=DSW_DOMAINS, clock=lambda: utc(2026, 1, 2, 3, 4, 5)
    )
    mock_person(router, person(42, FOUNDER))
    route = mock_interactions(router, {"emails": []})
    adapter.email_interactions(FOUNDER, utc(2025, 12, 1))
    assert route.calls.last.request.url.params["end_time"] == "2026-01-02T03:04:05Z"
    assert fixed_clock() == NOW
