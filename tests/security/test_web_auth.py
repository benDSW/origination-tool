"""R23 dashboard auth: every route is behind login, login is rate limited, sessions
are minimal, rotated and expire, and redirects stay on-site."""

from __future__ import annotations

import base64
import json
import logging
from datetime import timedelta
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.routing import Mount

from app.main import create_app
from app.web import Authenticator, LoginRateLimiter, PasswordAuthenticator
from app.web.auth import is_authenticated, safe_next
from app.web.security import PUBLIC_PATHS
from tests.web import conftest as web_fixtures
from tests.web.conftest import (
    NOW,
    PASSWORD,
    PASSWORD_HASH,
    SESSION_SECRET,
    FakeClock,
    WebClient,
    World,
    all_routes,
    concrete_path,
    make_settings,
)

# The dashboard fixtures (tests/security has no conftest of its own).
_app_loggers_enabled = web_fixtures._app_loggers_enabled
anon = web_fixtures.anon
client = web_fixtures.client
ids = web_fixtures.ids
world = web_fixtures.world

PUBLIC = {"/login", "/healthz"}


def protected_endpoints(app: Any) -> list[tuple[str, str]]:
    endpoints: list[tuple[str, str]] = []
    for route in all_routes(app.routes):
        if isinstance(route, Mount):
            assert route.path == "/static"  # the only mount, and it is public
            continue
        path = getattr(route, "path", "")
        if path in PUBLIC:
            continue
        for method in sorted(getattr(route, "methods", None) or ()):
            endpoints.append((method, concrete_path(path)))
    return endpoints


# --------------------------------------------------------------------------- #
# Every route requires auth
# --------------------------------------------------------------------------- #


def test_public_paths_are_exactly_login_and_healthz() -> None:
    assert frozenset(PUBLIC) == PUBLIC_PATHS


def test_every_non_public_route_rejects_anonymous_requests(world: World, anon: WebClient) -> None:
    endpoints = protected_endpoints(world.app)
    assert len(endpoints) >= 25, endpoints  # sanity: the walk found the dashboard
    assert any(p.startswith("/outreach/1/") for _, p in endpoints)
    for method, path in endpoints:
        response = anon.client.request(method, path, follow_redirects=False)
        if method in ("GET", "HEAD"):
            assert response.status_code == 303, (method, path, response.status_code)
            assert response.headers["location"].startswith("/login"), (method, path)
        else:
            assert response.status_code == 401, (method, path, response.status_code)
    assert world.store.latest_run() is None  # nothing ran
    assert world.catalog.created == []


def test_every_route_is_an_api_route_or_the_static_mount(world: World) -> None:
    routes = all_routes(world.app.routes)
    assert len(routes) >= 25
    for route in routes:
        assert isinstance(route, APIRoute | Mount), route


def test_unknown_paths_do_not_leak_to_anonymous_users(anon: WebClient) -> None:
    response = anon.client.get("/definitely-not-a-page", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_anonymous_post_does_not_reach_the_route(anon: WebClient, world: World, ids: dict[str, int]) -> None:
    anon.get("/login")  # has a valid (anonymous) CSRF token
    response = anon.post(f"/outreach/{ids['sig_test0001']}/sent")
    assert response.status_code == 401
    assert world.status(ids["sig_test0001"]).value == "drafted"


def test_healthz_and_static_are_public(anon: WebClient) -> None:
    assert anon.client.get("/healthz").json() == {"status": "ok"}
    assert anon.client.get("/static/app.css").status_code == 200
    assert anon.client.get("/static/app.js").status_code == 200


# --------------------------------------------------------------------------- #
# Login / logout
# --------------------------------------------------------------------------- #


def test_login_success_and_wrong_password(anon: WebClient) -> None:
    page = anon.get("/login")
    assert page.status_code == 200 and 'name="csrf_token"' in page.text and 'type="password"' in page.text
    wrong = anon.post("/login", {"password": "not it"})
    assert wrong.status_code == 401 and "Incorrect password." in wrong.text
    assert anon.client.get("/", follow_redirects=False).status_code == 303
    ok = anon.post("/login", {"password": PASSWORD})
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    assert anon.client.get("/").status_code == 200


def test_login_page_redirects_when_already_logged_in(client: WebClient) -> None:
    response = client.client.get("/login?next=/drafts", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/drafts"


def test_logout_clears_the_session(client: WebClient) -> None:
    assert client.post("/logout").status_code == 303
    assert client.client.get("/", follow_redirects=False).status_code == 303


def test_empty_password_hash_means_login_always_fails(world: World, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        app = create_app(services=world.services, settings=make_settings(admin_password_hash=""))
        web = WebClient(TestClient(app))
        assert web.login().status_code == 401
        assert web.login("").status_code in (400, 401)  # an empty field is rejected outright
        assert web.client.get("/", follow_redirects=False).status_code == 303
    assert "ADMIN_PASSWORD_HASH is not set" in caplog.text


def test_password_authenticator() -> None:
    auth = PasswordAuthenticator(
        PASSWORD_HASH, hasher=PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    assert isinstance(auth, Authenticator) and auth.configured
    assert auth.verify(PASSWORD) is True
    assert auth.verify("wrong") is False
    assert auth.verify("") is False
    assert auth.verify("x" * 2000) is False
    assert auth.verify(None) is False  # type: ignore[arg-type]
    broken = PasswordAuthenticator(
        "not-an-argon2-hash", hasher=PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    assert broken.verify(PASSWORD) is False
    unset = PasswordAuthenticator("", hasher=PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    assert not unset.configured and unset.verify(PASSWORD) is False


def test_failed_login_logs_no_password(anon: WebClient, caplog: pytest.LogCaptureFixture) -> None:
    anon.get("/login")
    with caplog.at_level(logging.INFO):
        anon.post("/login", {"password": "synthetic-wrong-password-123"})
    assert "Failed dashboard login" in caplog.text
    assert "synthetic-wrong-password-123" not in caplog.text


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


def test_login_rate_limit_five_failures_per_fifteen_minutes(anon: WebClient, world: World) -> None:
    anon.get("/login")
    for _ in range(5):
        assert anon.post("/login", {"password": "wrong"}).status_code == 401
    blocked = anon.post("/login", {"password": PASSWORD})  # even the right password
    assert blocked.status_code == 429 and "Too many failed attempts" in blocked.text
    world.clock.advance(minutes=15, seconds=1)
    assert anon.post("/login", {"password": PASSWORD}).status_code == 303


def test_successful_login_resets_the_failure_count(anon: WebClient) -> None:
    anon.get("/login")
    for _ in range(4):
        anon.post("/login", {"password": "wrong"})
    assert anon.login().status_code == 303
    anon.post("/logout")
    anon.get("/login")
    for _ in range(4):
        assert anon.post("/login", {"password": "wrong"}).status_code == 401
    assert anon.post("/login", {"password": PASSWORD}).status_code == 303


def test_rate_limiter_is_per_key_and_bounded() -> None:
    clock = FakeClock()
    limiter = LoginRateLimiter(clock, max_failures=2, window=timedelta(minutes=15), max_keys=3)
    for _ in range(2):
        limiter.record_failure("10.0.0.1")
    assert limiter.is_blocked("10.0.0.1") and not limiter.is_blocked("10.0.0.2")
    for n in range(2, 6):  # more keys than max_keys: the oldest is evicted, memory stays bounded
        limiter.record_failure(f"10.0.0.{n}")
    assert len(limiter._failures) <= 3
    clock.advance(minutes=16)
    limiter.record_failure("10.0.0.9")  # prunes expired keys first
    assert not limiter.is_blocked("10.0.0.5")
    limiter.reset("10.0.0.9")
    assert not limiter.is_blocked("10.0.0.9")


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


def _session_data(client: TestClient) -> dict[str, object]:
    cookie = client.cookies.get("dsw_session")
    assert cookie
    payload = TimestampSigner(SESSION_SECRET).unsign(cookie)
    return json.loads(base64.b64decode(payload))


def test_session_holds_only_user_csrf_login_time_sid_and_password_binding(client: WebClient) -> None:
    data = _session_data(client.client)
    assert set(data) == {"user", "csrf", "login_at", "sid", "pwv"}
    assert data["user"] == "admin" and data["login_at"] == int(NOW.timestamp())
    assert isinstance(data["sid"], str) and len(data["sid"]) >= 32  # random, revocable server-side
    assert PASSWORD not in json.dumps(data) and PASSWORD_HASH not in json.dumps(data)


def test_session_is_rotated_on_login(anon: WebClient, world: World) -> None:
    anon.get("/login")
    pre_login_token = anon.csrf
    assert set(_session_data(anon.client)) == {"csrf"}
    anon.post("/login", {"password": PASSWORD})
    assert _session_data(anon.client)["csrf"] != pre_login_token
    # The pre-login token no longer authorises anything.
    response = anon.client.post("/runs", data={"csrf_token": pre_login_token}, follow_redirects=False)
    assert response.status_code == 403 and world.store.latest_run() is None


def test_session_cookie_flags(anon: WebClient) -> None:
    anon.get("/login")
    response = anon.post("/login", {"password": PASSWORD})
    cookie = next(v for k, v in response.headers.multi_items() if k == "set-cookie" and "dsw_session" in v)
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=lax" in lowered and "max-age=43200" in lowered
    assert "secure" not in lowered  # not production


def test_production_session_cookie_is_secure(world: World) -> None:
    app = create_app(services=world.services, settings=make_settings(app_env="production"))
    web = WebClient(TestClient(app, base_url="https://testserver"))
    web.get("/login")
    response = web.post("/login", {"password": PASSWORD})
    cookie = next(v for k, v in response.headers.multi_items() if k == "set-cookie" and "dsw_session" in v)
    assert "secure" in cookie.lower()


def test_session_expires_after_the_absolute_lifetime(client: WebClient, world: World) -> None:
    world.clock.advance(hours=11, minutes=59)
    assert client.client.get("/", follow_redirects=False).status_code == 200
    world.clock.advance(minutes=2)
    response = client.client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert client.post("/runs").status_code == 401


def test_forged_session_cookie_is_ignored(anon: WebClient) -> None:
    payload = base64.b64encode(
        json.dumps({"user": "admin", "csrf": "x", "login_at": int(NOW.timestamp())}).encode()
    )
    forged = TimestampSigner("some-other-secret-that-is-long-enough").sign(payload).decode()
    anon.client.cookies.set("dsw_session", forged)
    assert anon.client.get("/", follow_redirects=False).status_code == 303


def test_is_authenticated_rejects_odd_sessions() -> None:
    now_ts = int(NOW.timestamp())
    assert not is_authenticated({}, NOW, 100)
    assert not is_authenticated({"user": "someone", "login_at": now_ts}, NOW, 100)
    assert not is_authenticated({"user": "admin", "login_at": "soon"}, NOW, 100)
    assert not is_authenticated({"user": "admin", "login_at": True}, NOW, 100)
    assert not is_authenticated({"user": "admin", "login_at": now_ts + 3600}, NOW, 100)  # from the future
    assert is_authenticated({"user": "admin", "login_at": now_ts - 50}, NOW, 100)


# --------------------------------------------------------------------------- #
# Open redirect
# --------------------------------------------------------------------------- #

OFF_SITE = [
    "//evil.example",
    "//evil.example/drafts",
    "https://evil.example",
    "http:/evil.example",
    "/\\evil.example",
    "\\\\evil.example",
    "javascript:alert(1)",
    "/\t/evil.example",
    "/\n/evil.example",
    " /drafts",
    "drafts",
    "",
    "/" + "a" * 3000,
]


@pytest.mark.parametrize("target", OFF_SITE)
def test_safe_next_rejects_off_site_targets(target: str) -> None:
    assert safe_next(target) == "/"


@pytest.mark.parametrize("target", ["/", "/drafts", "/outreach/3", "/pipeline?x=1", "/%2F%2Fevil.example"])
def test_safe_next_accepts_local_paths(target: str) -> None:
    assert safe_next(target) == target


@pytest.mark.parametrize("target", ["//evil.example", "https://evil.example", "/\\evil.example"])
def test_login_never_redirects_off_site(anon: WebClient, target: str) -> None:
    anon.get("/login")
    response = anon.post("/login", {"password": PASSWORD, "next": target})
    assert response.status_code == 303 and response.headers["location"] == "/"


def test_login_returns_to_the_requested_page(anon: WebClient) -> None:
    first = anon.client.get("/drafts?x=1", follow_redirects=False)
    assert first.headers["location"] == "/login?next=%2Fdrafts%3Fx%3D1"
    page = anon.get(first.headers["location"]).text
    assert 'name="next" value="/drafts?x=1"' in page
    response = anon.post("/login", {"password": PASSWORD, "next": "/drafts?x=1"})
    assert response.headers["location"] == "/drafts?x=1"
