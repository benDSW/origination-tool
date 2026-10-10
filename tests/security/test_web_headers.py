"""R23 response hardening: security headers everywhere, safe error pages, request
size limit, and GET handlers that never change state."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.domain.models import SavedSearch, SearchFilter
from app.main import create_app
from app.services import ExternalServiceError, InvalidAction, ServiceError, ValidationFailed
from app.web.security import CONTENT_SECURITY_POLICY, BodySizeLimitMiddleware
from tests.web import conftest as web_fixtures
from tests.web.conftest import (
    WebClient,
    World,
    all_routes,
    concrete_path,
    make_settings,
)

# The dashboard fixtures (tests/security has no conftest of its own).
anon = web_fixtures.anon
client = web_fixtures.client
ids = web_fixtures.ids
world = web_fixtures.world

NORTH = SavedSearch(
    external_id="sch_north",
    title="North founders",
    filters=(SearchFilter(key="country", values=("United Kingdom",)),),
)
EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


def assert_security_headers(response: Response, *, cacheable: bool = False) -> None:
    h = response.headers
    assert h["content-security-policy"] == EXPECTED_CSP
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    assert "camera=()" in h["permissions-policy"]
    if not cacheable:
        assert h["cache-control"] == "no-store"


def test_csp_is_the_specified_policy() -> None:
    assert CONTENT_SECURITY_POLICY == EXPECTED_CSP


def test_headers_on_public_responses(anon: WebClient) -> None:
    for path in ("/login", "/healthz"):
        response = anon.client.get(path)
        assert_security_headers(response)
        assert "strict-transport-security" not in response.headers  # not production
    redirect = anon.client.get("/drafts", follow_redirects=False)
    assert_security_headers(redirect)
    unauthorised = anon.client.post("/runs", follow_redirects=False)
    assert unauthorised.status_code == 401
    assert_security_headers(unauthorised)


def test_static_files_have_headers_and_correct_types(anon: WebClient) -> None:
    js = anon.client.get("/static/app.js")
    css = anon.client.get("/static/app.css")
    assert js.headers["content-type"].startswith("text/javascript")  # nosniff needs the right type
    assert css.headers["content-type"].startswith("text/css")
    for response in (js, css):
        assert_security_headers(response, cacheable=True)
        assert response.headers.get("cache-control") != "no-store"
    missing = anon.client.get("/static/nope.js")
    assert missing.status_code == 404
    assert_security_headers(missing, cacheable=True)


def test_authenticated_pages_are_not_cached(client: WebClient, ids: dict[str, int]) -> None:
    for path in ("/", "/drafts", "/pipeline", f"/outreach/{ids['sig_test0001']}", "/template", "/runs"):
        assert_security_headers(client.client.get(path))


def test_hsts_in_production(world: World) -> None:
    app = create_app(services=world.services, settings=make_settings(app_env="production"))
    response = TestClient(app, base_url="https://testserver").get("/healthz")
    assert response.headers["strict-transport-security"].startswith("max-age=31536000")
    assert_security_headers(response)


def test_unhandled_errors_render_a_bare_500_without_details(
    client: WebClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("synthetic-secret-detail-xyz")

    monkeypatch.setattr(world.services.outreach, "dashboard_summary", boom)
    response = client.client.get("/")
    assert response.status_code == 500
    assert "synthetic-secret-detail-xyz" not in response.text
    assert "RuntimeError" not in response.text and "Traceback" not in response.text
    assert_security_headers(response)


def test_404_never_echoes_the_path(client: WebClient) -> None:
    response = client.client.get("/no-such-page-<b>marker</b>")
    assert response.status_code == 404
    assert "marker" not in response.text
    assert_security_headers(response)


def test_validation_errors_never_echo_input(client: WebClient, world: World) -> None:
    world.catalog.searches.append(NORTH)
    (config,) = world.services.searches.sync_catalog()
    response = client.post(
        f"/searches/{config.id}", {"location_policy": "uk_outside_london", "lookback_days": "<i>evil</i>"}
    )
    assert response.status_code == 400
    assert "evil" not in response.text
    assert "Some of the submitted values were invalid." in response.text


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ExternalServiceError("Evertrace is down"), 502),
        (InvalidAction("Not allowed now"), 409),
        (ValidationFailed(["Bad thing"]), 400),
        (ServiceError("Generic problem"), 400),
    ],
)
def test_service_errors_become_user_safe_pages(
    client: WebClient, world: World, monkeypatch: pytest.MonkeyPatch, error: ServiceError, status: int
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(world.services.outreach, "list_by_status", fail)
    response = client.client.get("/drafts")
    assert response.status_code == status
    assert error.message in response.text
    assert_security_headers(response)


def test_oversized_request_bodies_are_rejected(client: WebClient, world: World) -> None:
    response = client.post("/template", {"subject": "s", "body": "x" * 300_000})
    assert response.status_code == 413
    assert world.services.templates.current().version == 1
    assert_security_headers(response)


def test_body_limit_also_applies_to_streamed_bodies() -> None:
    received: list[bytes] = []

    async def app(scope: dict, receive, send) -> None:  # type: ignore[no-untyped-def]
        while True:
            message = await receive()
            received.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def run(chunks: list[bytes], headers: list[tuple[bytes, bytes]]) -> int:
        messages = [
            {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
            for i, c in enumerate(chunks)
        ]
        sent: list[dict] = []

        async def receive() -> dict:
            return messages.pop(0)

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/x", "headers": headers}
        await BodySizeLimitMiddleware(app, max_bytes=10)(scope, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    assert asyncio.run(run([b"12345", b"12345"], [])) == 200
    assert asyncio.run(run([b"123456", b"123456"], [])) == 413  # chunked, no content-length
    assert asyncio.run(run([b"1"], [(b"content-length", b"999")])) == 413
    assert asyncio.run(run([b"1"], [(b"content-length", b"nonsense")])) == 413


def test_get_handlers_never_change_state(client: WebClient, world: World, ids: dict[str, int]) -> None:
    world.catalog.searches.append(NORTH)
    world.services.searches.sync_catalog()

    def snapshot() -> tuple[object, ...]:
        return (
            world.store.count_by_status(),
            [r.model_dump() for r in world.store.list_runs(50)],
            world.services.templates.current().version,
            [c.model_dump() for c in world.store.list_search_configs()],
            [v.outreach.model_dump() for v in world.store.list_outreach()],
            list(world.catalog.created),
            list(world.inbox.calls),
            list(world.finder.calls),
        )

    before = snapshot()
    oid = ids["sig_test0001"]
    gets = 0
    for route in all_routes(world.app.routes):
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods:
            continue
        path = concrete_path(route.path, str(oid))
        for method in sorted(methods & {"GET", "HEAD"}):
            response = client.client.request(method, path)
            assert response.status_code in (200, 303), (method, path, response.status_code)
            gets += 1
    assert gets >= 10
    assert snapshot() == before
