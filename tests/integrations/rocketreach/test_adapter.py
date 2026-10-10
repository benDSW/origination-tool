"""RocketReachAdapter: EmailFinder port behaviour end to end (respx-mocked, synthetic data)."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from app.domain.models import EmailLookupResult, LookupOutcome
from app.domain.ports import EmailFinder, IntegrationError
from app.integrations.rocketreach import RocketReachAdapter, RocketReachClient
from app.integrations.rocketreach.adapter import parse_remaining_credits
from tests.integrations.rocketreach.conftest import API_KEY, PROFILE_URL, SLUG, FakeTime, profile


@pytest.fixture
def adapter(client: RocketReachClient) -> RocketReachAdapter:
    return RocketReachAdapter(client)


def find(
    adapter: RocketReachAdapter, slug: str = SLUG, domain: str | None = "co1.example"
) -> EmailLookupResult:
    return adapter.find_email(linkedin_slug=slug, full_name="Alex Testfounder", company_domain=domain)


def test_implements_email_finder_port(adapter: RocketReachAdapter) -> None:
    assert isinstance(adapter, EmailFinder)
    assert API_KEY not in repr(adapter)


def test_from_api_key(fake_time: FakeTime, shared_http: httpx.Client) -> None:
    a = RocketReachAdapter.from_api_key(
        API_KEY, http=shared_http, sleep=fake_time.sleep, clock=fake_time.clock
    )
    assert isinstance(a, EmailFinder)
    a.close()


# --------------------------------------------------------------------------- #
# find_email
# --------------------------------------------------------------------------- #


def test_found_prefers_company_domain(api: respx.MockRouter, adapter: RocketReachAdapter) -> None:
    route = api.get("/person/lookup").mock(return_value=httpx.Response(200, json=profile()))
    result = find(adapter)
    assert result == EmailLookupResult(
        outcome=LookupOutcome.FOUND,
        email="alex@co1.example",
        grade="A",
        reason="professional email on the company domain",
    )
    assert route.calls.last.request.url.params["linkedin_url"] == PROFILE_URL


@pytest.mark.parametrize("slug", ["", "x", "bad slug", "../../account", "a/b", "alex\r\nX: y", "?q=1"])
def test_invalid_slug_rejected_without_http(
    api: respx.MockRouter, adapter: RocketReachAdapter, slug: str
) -> None:
    result = find(adapter, slug=slug)
    assert result.outcome is LookupOutcome.REJECTED
    assert result.reason == "invalid LinkedIn slug"
    assert not api.calls


def test_different_person_rejected(api: respx.MockRouter, adapter: RocketReachAdapter) -> None:
    api.get("/person/lookup").mock(
        return_value=httpx.Response(
            200, json=profile(linkedin_url="https://www.linkedin.com/in/someone-else-7")
        )
    )
    result = find(adapter)
    assert result.outcome is LookupOutcome.REJECTED
    assert result.email is None


def test_uk_subdomain_and_trailing_slash_accepted(api: respx.MockRouter, adapter: RocketReachAdapter) -> None:
    api.get("/person/lookup").mock(
        return_value=httpx.Response(
            200, json=profile(linkedin_url="http://uk.linkedin.com/in/Alex-Testfounder-1/?trk=x")
        )
    )
    assert find(adapter).outcome is LookupOutcome.FOUND


def test_no_verified_email_not_found(api: respx.MockRouter, adapter: RocketReachAdapter) -> None:
    api.get("/person/lookup").mock(
        return_value=httpx.Response(
            200,
            json=profile(
                emails=[
                    {"email": "alex@co1.example", "smtp_valid": "invalid", "grade": "A"},
                    {"email": "alex2@co1.example", "smtp_valid": "inconclusive", "grade": "B"},
                ]
            ),
        )
    )
    result = find(adapter)
    assert result.outcome is LookupOutcome.NOT_FOUND
    assert result.reason == "no verified / A-grade email"


def test_pending_then_complete(api: respx.MockRouter, adapter: RocketReachAdapter) -> None:
    api.get("/person/lookup").mock(
        return_value=httpx.Response(200, json=profile(status="searching", emails=[]))
    )
    api.get("/person/checkStatus").mock(return_value=httpx.Response(200, json=[profile()]))
    assert find(adapter).outcome is LookupOutcome.FOUND


def test_pending_timeout_is_error_for_later_retry(api: respx.MockRouter, adapter: RocketReachAdapter) -> None:
    api.get("/person/lookup").mock(return_value=httpx.Response(200, json=profile(status="searching")))
    api.get("/person/checkStatus").mock(return_value=httpx.Response(200, json=[profile(status="progress")]))
    result = find(adapter)
    assert result == EmailLookupResult(outcome=LookupOutcome.ERROR, reason="lookup still pending")


def test_failed_status_not_found(api: respx.MockRouter, adapter: RocketReachAdapter) -> None:
    api.get("/person/lookup").mock(return_value=httpx.Response(200, json=profile(status="failed")))
    result = find(adapter)
    assert result.outcome is LookupOutcome.NOT_FOUND
    assert result.email is None


def test_404_not_found(api: respx.MockRouter, adapter: RocketReachAdapter) -> None:
    api.get("/person/lookup").mock(return_value=httpx.Response(404))
    assert find(adapter).outcome is LookupOutcome.NOT_FOUND


def test_429_retry_then_found(
    api: respx.MockRouter, adapter: RocketReachAdapter, fake_time: FakeTime
) -> None:
    api.get("/person/lookup").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json=profile())]
    )
    assert find(adapter).outcome is LookupOutcome.FOUND
    assert 3.0 in fake_time.sleeps


def test_401_raises_non_retryable_without_key(
    api: respx.MockRouter, adapter: RocketReachAdapter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    api.get("/person/lookup").mock(return_value=httpx.Response(401, json={"detail": "Invalid API key"}))
    with pytest.raises(IntegrationError) as info:
        find(adapter)
    assert info.value.retryable is False
    assert API_KEY not in str(info.value)
    assert API_KEY not in caplog.text


def test_timeout_raises_integration_error(api: respx.MockRouter, adapter: RocketReachAdapter) -> None:
    api.get("/person/lookup").mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(IntegrationError) as info:
        find(adapter)
    assert info.value.retryable is True
    assert API_KEY not in str(info.value)


def test_logs_outcome_only(
    api: respx.MockRouter, adapter: RocketReachAdapter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    api.get("/person/lookup").mock(return_value=httpx.Response(200, json=profile()))
    find(adapter)
    assert "outcome=found" in caplog.text
    assert "alex@co1.example" not in caplog.text
    assert API_KEY not in caplog.text


# --------------------------------------------------------------------------- #
# Credits
# --------------------------------------------------------------------------- #


def account_payload(remaining: object) -> dict[str, object]:
    return {
        "credit_usage": [
            {"credit_type": "premium_lookup", "allocated": 0, "used": 0, "remaining": 0},
            {"credit_type": "standard_lookup", "allocated": 960, "used": 17, "remaining": remaining},
        ],
        "rate_limits": [{"action": "person_lookup", "duration": "minute", "limit": 15}],
    }


def test_remaining_credits(api: respx.MockRouter, adapter: RocketReachAdapter) -> None:
    route = api.get("/account/").mock(return_value=httpx.Response(200, json=account_payload(943)))
    assert adapter.remaining_credits() == 943
    assert route.calls.last.request.headers["Api-Key"] == API_KEY


def test_remaining_credits_inf_is_none(api: respx.MockRouter, adapter: RocketReachAdapter) -> None:
    api.get("/account/").mock(return_value=httpx.Response(200, json=account_payload("inf")))
    assert adapter.remaining_credits() is None


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (943, 943),
        (0, 0),
        (-3, 0),
        ("12", 12),
        (" 7 ", 7),
        (5.9, 5),
        ("inf", None),
        ("Infinity", None),
        (float("inf"), None),
        ("lots", None),
        (None, None),
        (True, None),
        ([1], None),
    ],
)
def test_parse_remaining_credits(remaining: object, expected: int | None) -> None:
    assert parse_remaining_credits(account_payload(remaining)) == expected


@pytest.mark.parametrize(
    "payload",
    [{}, {"credit_usage": "x"}, {"credit_usage": [{"credit_type": "other", "remaining": 5}, "junk"]}],
)
def test_parse_remaining_credits_unknown(payload: dict[str, object]) -> None:
    assert parse_remaining_credits(payload) is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(401),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["list"]),
    ],
)
def test_remaining_credits_never_raises(
    api: respx.MockRouter,
    adapter: RocketReachAdapter,
    response: httpx.Response,
    caplog: pytest.LogCaptureFixture,
) -> None:
    api.get("/account/").mock(return_value=response)
    assert adapter.remaining_credits() is None
    assert API_KEY not in caplog.text


def test_remaining_credits_timeout_none(api: respx.MockRouter, adapter: RocketReachAdapter) -> None:
    api.get("/account/").mock(side_effect=httpx.ReadTimeout("slow"))
    assert adapter.remaining_credits() is None


def test_remaining_credits_unconfigured_none(
    api: respx.MockRouter, fake_time: FakeTime, shared_http: httpx.Client
) -> None:
    a = RocketReachAdapter(
        RocketReachClient("", http=shared_http, sleep=fake_time.sleep, clock=fake_time.clock)
    )
    assert a.remaining_credits() is None
    assert not api.calls
