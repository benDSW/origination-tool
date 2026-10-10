"""R23 CSRF: every state-changing request needs the session's synchronizer token."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.web.csrf import SAFE_METHODS, tokens_match
from tests.web import conftest as web_fixtures
from tests.web.conftest import (
    PASSWORD,
    WebClient,
    World,
    all_routes,
    concrete_path,
)

# The dashboard fixtures (tests/security has no conftest of its own).
anon = web_fixtures.anon
client = web_fixtures.client
ids = web_fixtures.ids
world = web_fixtures.world

PUBLIC = {"/login", "/healthz"}


def unsafe_endpoints(app: Any) -> list[tuple[str, str]]:
    endpoints: list[tuple[str, str]] = []
    for route in all_routes(app.routes):
        for method in sorted(getattr(route, "methods", None) or ()):
            if method not in SAFE_METHODS and route.path not in PUBLIC:
                endpoints.append((method, concrete_path(route.path)))
    return endpoints


def _snapshot(world: World) -> tuple[object, ...]:
    return (
        world.store.count_by_status(),
        world.store.latest_run(),
        world.services.templates.current().version,
        [c.model_dump() for c in world.store.list_search_configs()],
        list(world.catalog.created),
    )


def test_every_unsafe_route_rejects_a_missing_or_wrong_token(
    client: WebClient, world: World, ids: dict[str, int]
) -> None:
    endpoints = unsafe_endpoints(world.app)
    assert len(endpoints) >= 15, endpoints
    before = _snapshot(world)
    for method, path in endpoints:
        missing = client.client.request(method, path, data={"confirm": "yes"}, follow_redirects=False)
        assert missing.status_code == 403, (method, path, missing.status_code)
        wrong = client.client.request(
            method, path, data={"csrf_token": "forged-token", "confirm": "yes"}, follow_redirects=False
        )
        assert wrong.status_code == 403, (method, path, wrong.status_code)
        header = client.client.request(
            method, path, headers={"X-CSRF-Token": "forged"}, follow_redirects=False
        )
        assert header.status_code == 403, (method, path, header.status_code)
    assert _snapshot(world) == before
    assert client.client.get("/").status_code == 200  # still logged in


def test_login_form_needs_the_pre_session_token(anon: WebClient) -> None:
    response = anon.client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 403
    anon.get("/login")
    assert anon.post("/login", {"password": PASSWORD}).status_code == 303


def test_token_from_another_session_is_rejected(world: World, client: WebClient, ids: dict[str, int]) -> None:
    other = WebClient(TestClient(world.app))
    other.login()
    assert other.csrf and other.csrf != client.csrf
    response = client.client.post(
        f"/outreach/{ids['sig_test0001']}/sent", data={"csrf_token": other.csrf}, follow_redirects=False
    )
    assert response.status_code == 403
    assert world.status(ids["sig_test0001"]).value == "drafted"


def test_header_token_is_accepted_for_fetch(client: WebClient) -> None:
    response = client.client.post(
        "/template/preview",
        data={"subject": "s", "body": "b"},
        headers={"X-CSRF-Token": client.csrf, "Accept": "application/json"},
    )
    assert response.status_code == 200 and response.json()["ok"] is True


def test_rejection_as_json_for_fetch(client: WebClient) -> None:
    response = client.client.post(
        "/template/preview", data={"subject": "s", "body": "b"}, headers={"Accept": "application/json"}
    )
    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": response.json()["error"]}


def test_multipart_form_with_token_is_accepted(client: WebClient, world: World, ids: dict[str, int]) -> None:
    oid = ids["sig_test0001"]
    response = client.client.post(
        f"/outreach/{oid}/sent",
        data={"csrf_token": client.csrf},
        files={"ignored": ("a.txt", b"x", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert world.status(oid).value == "sent"


def test_every_form_on_every_page_carries_the_token(client: WebClient, ids: dict[str, int]) -> None:
    pages = [
        "/",
        "/drafts",
        "/needs-email",
        "/excluded",
        "/pipeline",
        "/searches",
        "/searches/new",
        "/template",
    ]
    pages += [f"/outreach/{oid}" for oid in ids.values()]
    forms = 0
    for page in pages:
        text = client.get(page).text
        chunks = text.split('<form method="post"')[1:]
        for chunk in chunks:
            forms += 1
            body = chunk.split("</form>")[0]
            assert f'name="csrf_token" value="{client.csrf}"' in body, page
    assert forms >= 20


@pytest.mark.parametrize(
    ("expected", "supplied", "ok"),
    [
        ("abc", "abc", True),
        ("abc", "abd", False),
        ("abc", None, False),
        (None, "abc", False),
        ("", "", False),
        ("abc", "a" * 500, False),
        ("abc", ["abc"], False),
    ],
)
def test_tokens_match(expected: object, supplied: object, ok: bool) -> None:
    assert tokens_match(expected, supplied) is ok
