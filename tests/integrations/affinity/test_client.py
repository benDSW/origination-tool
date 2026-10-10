"""AffinityClient: auth, retries, error hygiene and SSRF-safe pagination (synthetic data only)."""

from __future__ import annotations

import base64
import inspect

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.domain.ports import IntegrationError
from app.integrations.affinity import client as client_module
from app.integrations.affinity.client import AffinityClient, is_trusted_url
from tests.integrations.affinity.conftest import FAKE_KEY

V2 = "https://api.affinity.co/v2"
V1 = "https://api.affinity.co"


# --------------------------------------------------------------------------- #
# Construction / auth
# --------------------------------------------------------------------------- #


def test_empty_key_is_rejected() -> None:
    with pytest.raises(IntegrationError) as exc:
        AffinityClient("")
    assert exc.value.retryable is False


def test_secretstr_key_accepted_and_repr_hides_key(router: respx.MockRouter) -> None:
    route = router.get(f"{V2}/lists/1").respond(json={"id": 1})
    with AffinityClient(SecretStr(FAKE_KEY)) as c:
        assert FAKE_KEY not in repr(c)
        c.get_v2("/lists/1", label="list")
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {FAKE_KEY}"


def test_v1_uses_basic_auth_with_empty_username(router: respx.MockRouter, client: AffinityClient) -> None:
    route = router.get(f"{V1}/persons").respond(json={"persons": []})
    client.get_v1("/persons", [("term", "x@example.com")], label="persons")
    expected = base64.b64encode(f":{FAKE_KEY}".encode()).decode()
    assert route.calls.last.request.headers["Authorization"] == f"Basic {expected}"


def test_client_exposes_no_write_methods() -> None:
    for name in ("post", "put", "patch", "delete", "request"):
        assert not hasattr(AffinityClient, name)
    source = inspect.getsource(client_module)
    for verb in (".post(", ".put(", ".patch(", ".delete(", ".request(", ".stream("):
        assert verb not in source


# --------------------------------------------------------------------------- #
# Errors and retries
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_not_retried(
    router: respx.MockRouter, client: AffinityClient, sleeps: list[float], status: int
) -> None:
    route = router.get(f"{V2}/lists/1").respond(status, json={"message": "SECRET-BODY-MARKER"})
    with pytest.raises(IntegrationError) as exc:
        client.get_v2("/lists/1", label="list")
    assert exc.value.retryable is False
    assert exc.value.system == "affinity"
    assert route.call_count == 1
    assert sleeps == []
    assert "SECRET-BODY-MARKER" not in str(exc.value)
    assert FAKE_KEY not in str(exc.value)


def test_other_4xx_is_not_retried(router: respx.MockRouter, client: AffinityClient) -> None:
    router.get(f"{V2}/lists/1").respond(404, text="SECRET-BODY-MARKER")
    with pytest.raises(IntegrationError) as exc:
        client.get_v2("/lists/1", label="list")
    assert exc.value.retryable is False
    assert "404" in str(exc.value)
    assert "SECRET-BODY-MARKER" not in str(exc.value)


def test_429_honours_retry_after_then_succeeds(
    router: respx.MockRouter, client: AffinityClient, sleeps: list[float]
) -> None:
    router.get(f"{V2}/lists/1").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429, headers={"Retry-After": "9999"}),  # capped
            httpx.Response(200, json={"id": 1, "name": "Pipeline"}),
        ]
    )
    assert client.get_v2("/lists/1", label="list")["name"] == "Pipeline"
    assert sleeps == [7.0, client_module.MAX_RETRY_AFTER_SECONDS]


def test_unparseable_retry_after_falls_back_to_backoff(
    router: respx.MockRouter, client: AffinityClient, sleeps: list[float]
) -> None:
    router.get(f"{V2}/lists/1").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "soon"}), httpx.Response(200, json={})]
    )
    client.get_v2("/lists/1", label="list")
    assert sleeps == [1.0]


def test_5xx_retries_with_exponential_backoff_then_fails(
    router: respx.MockRouter, client: AffinityClient, sleeps: list[float]
) -> None:
    route = router.get(f"{V2}/lists/1").respond(503, text="SECRET-BODY-MARKER")
    with pytest.raises(IntegrationError) as exc:
        client.get_v2("/lists/1", label="list")
    assert exc.value.retryable is True
    assert route.call_count == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert "SECRET-BODY-MARKER" not in str(exc.value)


def test_timeout_is_retried_then_raises_retryable(
    router: respx.MockRouter, client: AffinityClient, sleeps: list[float]
) -> None:
    router.get(f"{V2}/lists/1").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(IntegrationError) as exc:
        client.get_v2("/lists/1", label="list")
    assert exc.value.retryable is True
    assert "timed out" in str(exc.value)
    assert len(sleeps) == 3


def test_network_error_does_not_leak_url(
    router: respx.MockRouter, sleeps: list[float], shared_http: httpx.Client
) -> None:
    router.get(f"{V1}/persons").mock(side_effect=httpx.ConnectError("boom founder@example.com"))
    c = AffinityClient(FAKE_KEY, http=shared_http, sleep=sleeps.append, max_retries=0)
    with pytest.raises(IntegrationError) as exc:
        c.get_v1("/persons", [("term", "founder@example.com")], label="persons")
    assert "network error" in str(exc.value)
    assert "founder@example.com" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    assert sleeps == []


def test_invalid_json_raises(router: respx.MockRouter, client: AffinityClient) -> None:
    router.get(f"{V2}/lists/1").respond(200, text="<html>SECRET-BODY-MARKER</html>")
    with pytest.raises(IntegrationError) as exc:
        client.get_v2("/lists/1", label="list")
    assert "not valid JSON" in str(exc.value)
    assert "SECRET-BODY-MARKER" not in str(exc.value)


def test_non_object_json_raises(router: respx.MockRouter, client: AffinityClient) -> None:
    router.get(f"{V2}/lists/1").respond(200, json=[1, 2, 3])
    with pytest.raises(IntegrationError, match="unexpected response shape"):
        client.get_v2("/lists/1", label="list")


def test_redirect_is_not_followed(router: respx.MockRouter, client: AffinityClient) -> None:
    router.get(f"{V2}/lists/1").respond(302, headers={"Location": "https://attacker.example/"})
    with pytest.raises(IntegrationError, match="unexpected HTTP 302"):
        client.get_v2("/lists/1", label="list")


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #


def test_get_refuses_untrusted_url_without_sending(router: respx.MockRouter, client: AffinityClient) -> None:
    with pytest.raises(IntegrationError, match="refused untrusted URL"):
        client._get("https://attacker.example/x", (), version=2, label="x")
    assert not router.calls


@pytest.mark.parametrize(
    ("url", "trusted"),
    [
        ("https://api.affinity.co/v2/lists/1/list-entries?cursor=abc", True),
        ("http://api.affinity.co/v2/lists", False),
        ("https://attacker.example/v2/lists", False),
        ("https://api.affinity.co.attacker.example/v2", False),
        ("https://api.affinity.co@attacker.example/v2", False),
        ("https://api.affinity.co:8443/v2", False),
        ("/v2/lists/1", False),
        ("https://api.affinity.co/v2/" + chr(1), False),  # control char -> InvalidURL
        ("", False),
    ],
)
def test_is_trusted_url(url: str, trusted: bool) -> None:
    assert is_trusted_url(url) is trusted


def test_is_trusted_url_rejects_non_strings() -> None:
    assert is_trusted_url(None) is False  # type: ignore[arg-type]


def test_v2_pagination_follows_trusted_next_url(router: respx.MockRouter, client: AffinityClient) -> None:
    next_url = f"{V2}/lists/1/list-entries?cursor=page2"
    first = router.get(f"{V2}/lists/1/list-entries", params={"limit": "100"}).respond(
        json={"data": [{"id": 1}], "pagination": {"nextUrl": next_url}}
    )
    second = router.get(f"{V2}/lists/1/list-entries", params={"cursor": "page2"}).respond(
        json={"data": [{"id": 2}], "pagination": {"nextUrl": None}}
    )
    pages = list(client.iter_v2_pages("/lists/1/list-entries", [("limit", 100)], label="list-entries"))
    assert [p["data"][0]["id"] for p in pages] == [1, 2]
    assert first.call_count == 1 and second.call_count == 1
    assert second.calls.last.request.headers["Authorization"] == f"Bearer {FAKE_KEY}"


def test_v2_pagination_tolerates_missing_pagination(router: respx.MockRouter, client: AffinityClient) -> None:
    router.get(f"{V2}/lists/1/list-entries").respond(json={"data": [], "pagination": "weird"})
    assert len(list(client.iter_v2_pages("/lists/1/list-entries", label="list-entries"))) == 1


def test_v2_pagination_refuses_foreign_next_url(router: respx.MockRouter, client: AffinityClient) -> None:
    router.get(f"{V2}/lists/1/list-entries").respond(
        json={"data": [], "pagination": {"nextUrl": "https://attacker.example/steal"}}
    )
    foreign = router.get("https://attacker.example/steal").respond(json={})
    with pytest.raises(IntegrationError, match="untrusted pagination URL") as exc:
        list(client.iter_v2_pages("/lists/1/list-entries", label="list-entries"))
    assert exc.value.retryable is False
    assert not foreign.called


def test_v2_pagination_hard_cap(
    router: respx.MockRouter, client: AffinityClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "MAX_PAGES", 3)
    route = router.get(f"{V2}/lists/1/list-entries").respond(
        json={"data": [], "pagination": {"nextUrl": f"{V2}/lists/1/list-entries?cursor=again"}}
    )
    with pytest.raises(IntegrationError, match="exceeded 3 pages"):
        list(client.iter_v2_pages("/lists/1/list-entries", label="list-entries"))
    assert route.call_count == 3


def test_v1_pagination_follows_page_token(router: respx.MockRouter, client: AffinityClient) -> None:
    route = router.get(f"{V1}/interactions").mock(
        side_effect=[
            httpx.Response(200, json={"emails": [{"id": 1}, "junk"], "next_page_token": "tok2"}),
            httpx.Response(200, json={"emails": [{"id": 2}], "next_page_token": None}),
        ]
    )
    items = list(client.iter_v1_pages("/interactions", [("type", 3)], label="i", items_key="emails"))
    assert [i["id"] for i in items] == [1, 2]
    assert "page_token" not in route.calls[0].request.url.params
    assert route.calls[1].request.url.params["page_token"] == "tok2"


def test_v1_pagination_hard_cap(
    router: respx.MockRouter, client: AffinityClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "MAX_PAGES", 2)
    router.get(f"{V1}/interactions").respond(json={"emails": [], "next_page_token": "forever"})
    with pytest.raises(IntegrationError, match="exceeded 2 pages"):
        list(client.iter_v1_pages("/interactions", label="i", items_key="emails"))
