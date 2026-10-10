"""RocketReach HTTP client: auth, retries, throttling, polling. respx-mocked, synthetic data."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.domain.ports import IntegrationError
from app.integrations.rocketreach import client as client_module
from app.integrations.rocketreach.client import RocketReachClient, Throttle
from tests.integrations.rocketreach.conftest import API_KEY, PROFILE_URL, FakeTime, profile


def make(fake_time: FakeTime, http: httpx.Client, **kw: object) -> RocketReachClient:
    return RocketReachClient(
        API_KEY,
        http=http,
        sleep=fake_time.sleep,
        clock=fake_time.clock,
        **kw,  # type: ignore[arg-type]
    )


def assert_secret_free(exc: BaseException) -> None:
    text = f"{exc} {exc!r} {exc.args}"
    assert API_KEY not in text
    assert "SYNTHETIC-BODY" not in text
    assert exc.__cause__ is None


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


def test_lookup_complete_sends_key_header_and_encoded_linkedin_url(
    api: respx.MockRouter, client: RocketReachClient
) -> None:
    route = api.get("/person/lookup").mock(return_value=httpx.Response(200, json=profile()))
    result = client.lookup_person(PROFILE_URL)

    assert result is not None and result["status"] == "complete"
    request = route.calls.last.request
    assert request.headers["Api-Key"] == API_KEY
    assert request.url.host == "api.rocketreach.co"
    assert request.url.path == "/api/v2/person/lookup"
    assert request.url.params["linkedin_url"] == PROFILE_URL
    assert "name" not in request.url.params  # never searched by name
    # Query is URL-encoded: '/' inside the value cannot be confused with the path.
    assert b"linkedin_url=https%3A" in request.url.raw_path


def test_lookup_accepts_single_profile_list(api: respx.MockRouter, client: RocketReachClient) -> None:
    api.get("/person/lookup").mock(return_value=httpx.Response(200, json=[profile()]))
    assert client.lookup_person(PROFILE_URL)["id"] == 4242  # type: ignore[index]


def test_lookup_404_returns_none(api: respx.MockRouter, client: RocketReachClient) -> None:
    api.get("/person/lookup").mock(return_value=httpx.Response(404, json={"detail": "not found"}))
    assert client.lookup_person(PROFILE_URL) is None


def test_failed_status_is_returned_without_polling(
    api: respx.MockRouter, client: RocketReachClient, fake_time: FakeTime
) -> None:
    api.get("/person/lookup").mock(return_value=httpx.Response(200, json=profile(status="failed", emails=[])))
    poll = api.get("/person/checkStatus")
    assert client.lookup_person(PROFILE_URL)["status"] == "failed"  # type: ignore[index]
    assert not poll.called
    assert fake_time.sleeps == []


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #


def test_pending_then_poll_until_complete(
    api: respx.MockRouter, client: RocketReachClient, fake_time: FakeTime
) -> None:
    api.get("/person/lookup").mock(
        return_value=httpx.Response(200, json=profile(status="searching", emails=None))
    )
    poll = api.get("/person/checkStatus").mock(
        side_effect=[
            httpx.Response(200, json=[profile(status="progress", emails=None)]),
            httpx.Response(200, json=[{"id": 9999, "status": "complete"}, profile()]),
        ]
    )
    result = client.lookup_person(PROFILE_URL)

    assert result is not None and result["status"] == "complete"
    assert result["emails"][0]["email"] == "alex@co1.example"
    assert poll.call_count == 2
    assert poll.calls.last.request.url.params["ids"] == "4242"
    assert poll.calls.last.request.headers["Api-Key"] == API_KEY
    assert fake_time.sleeps == [2.0, 2.0]


def test_pending_poll_gives_up_after_budget(
    api: respx.MockRouter, client: RocketReachClient, fake_time: FakeTime
) -> None:
    api.get("/person/lookup").mock(return_value=httpx.Response(200, json=profile(status="waiting")))
    poll = api.get("/person/checkStatus").mock(
        return_value=httpx.Response(200, json=[profile(status="searching")])
    )
    result = client.lookup_person(PROFILE_URL)

    assert result is not None and result["status"] == "searching"
    assert poll.call_count == 15  # 30s budget / 2s interval
    assert sum(fake_time.sleeps) == pytest.approx(30.0)


def test_poll_merges_slim_status_record(api: respx.MockRouter, client: RocketReachClient) -> None:
    api.get("/person/lookup").mock(return_value=httpx.Response(200, json=profile(status="searching")))
    # Single record with no id: treated as the one we asked about; earlier fields kept.
    api.get("/person/checkStatus").mock(return_value=httpx.Response(200, json=[{"status": "complete"}]))
    result = client.lookup_person(PROFILE_URL)
    assert result is not None
    assert result["status"] == "complete" and result["linkedin_url"] == PROFILE_URL


def test_poll_ignores_unrelated_records(
    api: respx.MockRouter, fake_time: FakeTime, shared_http: httpx.Client
) -> None:
    c = make(fake_time, shared_http, poll_timeout=4.0)
    api.get("/person/lookup").mock(return_value=httpx.Response(200, json=profile(status="searching")))
    api.get("/person/checkStatus").mock(
        return_value=httpx.Response(
            200, json=[{"id": 1, "status": "complete"}, {"id": 2, "status": "complete"}]
        )
    )
    assert c.lookup_person(PROFILE_URL)["status"] == "searching"  # type: ignore[index]


def test_poll_404_returns_none(api: respx.MockRouter, client: RocketReachClient) -> None:
    api.get("/person/lookup").mock(return_value=httpx.Response(200, json=profile(status="searching")))
    api.get("/person/checkStatus").mock(return_value=httpx.Response(404))
    assert client.lookup_person(PROFILE_URL) is None


@pytest.mark.parametrize("bad_id", [None, "4242", True])
def test_pending_without_usable_id_is_returned_as_is(
    api: respx.MockRouter, client: RocketReachClient, bad_id: object
) -> None:
    api.get("/person/lookup").mock(
        return_value=httpx.Response(200, json=profile(status="searching", id=bad_id))
    )
    poll = api.get("/person/checkStatus")
    assert client.lookup_person(PROFILE_URL)["status"] == "searching"  # type: ignore[index]
    assert not poll.called


def test_check_status_shapes(api: respx.MockRouter, client: RocketReachClient) -> None:
    route = api.get("/person/checkStatus")
    route.mock(return_value=httpx.Response(200, json={"profiles": [{"id": 1}, "junk"]}))
    assert client.check_status(1) == [{"id": 1}]
    route.mock(return_value=httpx.Response(200, json={"id": 1, "status": "complete"}))
    assert client.check_status(1) == [{"id": 1, "status": "complete"}]
    route.mock(return_value=httpx.Response(200, json="nope"))
    with pytest.raises(IntegrationError, match="unexpected response shape"):
        client.check_status(1)


# --------------------------------------------------------------------------- #
# Errors and retries
# --------------------------------------------------------------------------- #


def test_429_honours_retry_after_then_succeeds(
    api: respx.MockRouter, client: RocketReachClient, fake_time: FakeTime
) -> None:
    route = api.get("/person/lookup").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}, text="SYNTHETIC-BODY"),
            httpx.Response(200, json=profile()),
        ]
    )
    assert client.lookup_person(PROFILE_URL) is not None
    assert route.call_count == 2
    assert fake_time.sleeps == [7.0]


def test_429_exhausted_raises_retryable(
    api: respx.MockRouter, client: RocketReachClient, fake_time: FakeTime
) -> None:
    route = api.get("/person/lookup").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"}, text="SYNTHETIC-BODY")
    )
    with pytest.raises(IntegrationError) as info:
        client.lookup_person(PROFILE_URL)
    assert route.call_count == 3  # initial + 2 retries
    assert info.value.retryable is True
    assert info.value.system == "rocketreach"
    assert "429" in str(info.value)
    assert_secret_free(info.value)


@pytest.mark.parametrize(
    ("header", "expected"),
    [("600", 60.0), ("not-a-number", 1.0), ("-5", 1.0), ("nan", 1.0), (None, 1.0), ("0.5", 0.5)],
)
def test_retry_after_parsing(
    api: respx.MockRouter, fake_time: FakeTime, shared_http: httpx.Client, header: str | None, expected: float
) -> None:
    c = make(fake_time, shared_http, min_lookup_interval=0.0)
    headers = {"Retry-After": header} if header is not None else {}
    api.get("/person/lookup").mock(
        side_effect=[httpx.Response(429, headers=headers), httpx.Response(200, json=profile())]
    )
    c.lookup_person(PROFILE_URL)
    assert fake_time.sleeps == [expected]


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_not_retried(
    api: respx.MockRouter, client: RocketReachClient, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    route = api.get("/person/lookup").mock(
        return_value=httpx.Response(status, json={"detail": f"bad key {API_KEY} SYNTHETIC-BODY"})
    )
    with pytest.raises(IntegrationError) as info:
        client.lookup_person(PROFILE_URL)
    assert route.call_count == 1
    assert info.value.retryable is False
    assert str(status) in str(info.value)
    assert_secret_free(info.value)
    assert API_KEY not in caplog.text


def test_5xx_retried_with_backoff_then_succeeds(
    api: respx.MockRouter, client: RocketReachClient, fake_time: FakeTime
) -> None:
    api.get("/person/lookup").mock(
        side_effect=[httpx.Response(502), httpx.Response(503), httpx.Response(200, json=profile())]
    )
    assert client.lookup_person(PROFILE_URL) is not None
    # Exponential backoff (1s, 2s); each retry is still a lookup, so the 4.1s throttle tops it up.
    assert fake_time.sleeps == [1.0, pytest.approx(3.1), 2.0, pytest.approx(2.1)]


def test_5xx_backoff_without_throttle(
    api: respx.MockRouter, fake_time: FakeTime, shared_http: httpx.Client
) -> None:
    c = make(fake_time, shared_http, min_lookup_interval=0.0, backoff_base=0.5)
    api.get("/person/lookup").mock(
        side_effect=[httpx.Response(500), httpx.Response(500), httpx.Response(200, json=profile())]
    )
    assert c.lookup_person(PROFILE_URL) is not None
    assert fake_time.sleeps == [0.5, 1.0]


def test_5xx_exhausted_raises_retryable(api: respx.MockRouter, client: RocketReachClient) -> None:
    route = api.get("/person/lookup").mock(return_value=httpx.Response(500, text="SYNTHETIC-BODY"))
    with pytest.raises(IntegrationError) as info:
        client.lookup_person(PROFILE_URL)
    assert route.call_count == 3
    assert info.value.retryable is True
    assert_secret_free(info.value)


def test_timeout_retried_then_raises(
    api: respx.MockRouter, client: RocketReachClient, caplog: pytest.LogCaptureFixture
) -> None:
    route = api.get("/person/lookup").mock(side_effect=httpx.ReadTimeout("read timed out"))
    with pytest.raises(IntegrationError) as info:
        client.lookup_person(PROFILE_URL)
    assert route.call_count == 3
    assert info.value.retryable is True
    assert "timeout" in str(info.value)
    assert_secret_free(info.value)
    assert API_KEY not in caplog.text


def test_timeout_then_success(api: respx.MockRouter, client: RocketReachClient) -> None:
    api.get("/person/lookup").mock(
        side_effect=[httpx.ConnectTimeout("slow"), httpx.Response(200, json=profile())]
    )
    assert client.lookup_person(PROFILE_URL) is not None


def test_transport_error_retried_then_raises(api: respx.MockRouter, client: RocketReachClient) -> None:
    api.get("/person/lookup").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(IntegrationError, match="ConnectError") as info:
        client.lookup_person(PROFILE_URL)
    assert_secret_free(info.value)


def test_transport_error_then_success(api: respx.MockRouter, client: RocketReachClient) -> None:
    api.get("/person/lookup").mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json=profile())]
    )
    assert client.lookup_person(PROFILE_URL) is not None


def test_other_4xx_not_retried(api: respx.MockRouter, client: RocketReachClient) -> None:
    route = api.get("/person/lookup").mock(return_value=httpx.Response(400, text="SYNTHETIC-BODY"))
    with pytest.raises(IntegrationError) as info:
        client.lookup_person(PROFILE_URL)
    assert route.call_count == 1
    assert info.value.retryable is False
    assert_secret_free(info.value)


def test_invalid_json(api: respx.MockRouter, client: RocketReachClient) -> None:
    api.get("/person/lookup").mock(return_value=httpx.Response(200, text="<html>SYNTHETIC-BODY</html>"))
    with pytest.raises(IntegrationError, match="invalid JSON") as info:
        client.lookup_person(PROFILE_URL)
    assert_secret_free(info.value)


@pytest.mark.parametrize("payload", ["just a string", [1, 2], [], 42])
def test_unexpected_lookup_shape(api: respx.MockRouter, client: RocketReachClient, payload: object) -> None:
    api.get("/person/lookup").mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(IntegrationError, match="unexpected response shape"):
        client.lookup_person(PROFILE_URL)


def test_account_requires_mapping(api: respx.MockRouter, client: RocketReachClient) -> None:
    api.get("/account/").mock(return_value=httpx.Response(200, json=[1]))
    with pytest.raises(IntegrationError):
        client.account()


def test_redirect_not_followed(api: respx.MockRouter, client: RocketReachClient) -> None:
    """A redirect could forward the Api-Key header to another host: never follow one."""
    api.get("/person/lookup").mock(
        return_value=httpx.Response(302, headers={"Location": "https://elsewhere.example/steal"})
    )
    with pytest.raises(IntegrationError, match="302"):
        client.lookup_person(PROFILE_URL)


@pytest.mark.parametrize("key", ["", "   "])
def test_unconfigured_key_raises_without_http(
    api: respx.MockRouter, fake_time: FakeTime, shared_http: httpx.Client, key: str
) -> None:
    c = RocketReachClient(key, http=shared_http, sleep=fake_time.sleep, clock=fake_time.clock)
    assert not c.configured
    with pytest.raises(IntegrationError) as info:
        c.lookup_person(PROFILE_URL)
    assert info.value.retryable is False
    assert not api.calls


def test_accepts_secretstr_and_repr_hides_key(
    api: respx.MockRouter, fake_time: FakeTime, shared_http: httpx.Client
) -> None:
    from pydantic import SecretStr

    c = RocketReachClient(SecretStr(API_KEY), http=shared_http, sleep=fake_time.sleep, clock=fake_time.clock)
    route = api.get("/account/").mock(return_value=httpx.Response(200, json={}))
    c.account()
    assert route.calls.last.request.headers["Api-Key"] == API_KEY
    assert API_KEY not in repr(c)
    assert API_KEY not in str(vars(c))


# --------------------------------------------------------------------------- #
# Throttling + lifecycle
# --------------------------------------------------------------------------- #


def test_throttle_spaces_lookups_with_injected_sleep(
    api: respx.MockRouter, client: RocketReachClient, fake_time: FakeTime
) -> None:
    api.get("/person/lookup").mock(return_value=httpx.Response(200, json=profile()))
    client.lookup_person(PROFILE_URL)
    client.lookup_person(PROFILE_URL)
    fake_time.now += 1.0  # 1s of "work" between calls
    client.lookup_person(PROFILE_URL)
    assert fake_time.sleeps == [pytest.approx(4.1), pytest.approx(3.1)]


def test_account_and_polls_are_not_lookup_throttled(
    api: respx.MockRouter, client: RocketReachClient, fake_time: FakeTime
) -> None:
    api.get("/account/").mock(return_value=httpx.Response(200, json={}))
    client.account()
    client.account()
    assert fake_time.sleeps == []


def test_throttle_unit() -> None:
    t = FakeTime()
    throttle = Throttle(4.1, sleep=t.sleep, clock=t.clock)
    throttle.wait()
    assert t.sleeps == []
    t.now += 10
    throttle.wait()
    assert t.sleeps == []
    t.now += 2
    throttle.wait()
    assert t.sleeps == [pytest.approx(2.1)]


class _StubHttp:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.is_closed = False

    def close(self) -> None:
        self.is_closed = True


def test_owned_http_is_configured_safely_and_closed(
    monkeypatch: pytest.MonkeyPatch, fake_time: FakeTime
) -> None:
    monkeypatch.setattr(client_module.httpx, "Client", _StubHttp)
    with RocketReachClient(API_KEY, sleep=fake_time.sleep, timeout=9.0) as owned:
        http = owned._http
    assert isinstance(http, _StubHttp)
    assert http.kwargs == {"timeout": 9.0, "follow_redirects": False}
    assert http.is_closed


def test_injected_http_is_not_closed(shared_http: httpx.Client) -> None:
    c = RocketReachClient(API_KEY, http=shared_http)
    c.close()
    assert not shared_http.is_closed  # caller owns it


def test_injected_http_still_uses_hard_coded_base_url(fake_time: FakeTime) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(
        base_url="https://elsewhere.example", transport=httpx.MockTransport(handler)
    ) as injected:
        c = RocketReachClient(API_KEY, http=injected, sleep=fake_time.sleep, clock=fake_time.clock)
        c.account()
    assert seen[0].url.host == "api.rocketreach.co"
    assert seen[0].url.path == "/api/v2/account/"
    # Every request carries an explicit timeout.
    assert seen[0].extensions["timeout"]["read"] == 15.0
