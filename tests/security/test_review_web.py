"""Regression tests for the security review, dashboard side:

3. the login rate limit cannot be bypassed by spoofing X-Forwarded-For, and a
   global failure budget caps distributed guessing;
4. unauthenticated Argon2 work is bounded (concurrency gate, atomic counting,
   CSRF rotation after a failure, explicit OWASP parameters);
7. sessions are revocable server-side (logout, "log out everywhere", password change).

Synthetic data only; no real network.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest
from argon2 import PasswordHasher, extract_parameters
from fastapi.testclient import TestClient

from app.jobs import hash_password
from app.main import create_app
from app.web import LoginRateLimiter, PasswordAuthenticator, VerificationGate
from app.web.auth import (
    ARGON2_MEMORY_COST_KIB,
    ARGON2_PARALLELISM,
    ARGON2_TIME_COST,
    client_key,
    is_authenticated,
)
from tests.web import conftest as web_fixtures
from tests.web.conftest import NOW, PASSWORD, FakeClock, WebClient, World, make_settings

# The dashboard fixtures (tests/security has no conftest of its own).
_app_loggers_enabled = web_fixtures._app_loggers_enabled
anon = web_fixtures.anon
client = web_fixtures.client
world = web_fixtures.world

EDGE = "203.0.113.7"  # what Railway's edge appends: the real client address (TEST-NET-3)


def _login(web: WebClient, password: str, forwarded_for: str | None = None) -> Any:
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
    return web.post("/login", {"password": password}, headers=headers)


# --------------------------------------------------------------------------- #
# Finding 3: X-Forwarded-For spoofing
# --------------------------------------------------------------------------- #


def test_rotating_a_spoofed_forwarded_for_does_not_bypass_the_rate_limit(anon: WebClient) -> None:
    """The audit's reproduction: 30 wrong passwords, each claiming a new leftmost
    X-Forwarded-For hop. Before the fix: 30 x 401 and 0 x 429."""
    anon.get("/login")
    codes = [_login(anon, "wrong", f"10.9.{i}.1, {EDGE}").status_code for i in range(30)]
    assert codes[:5] == [401] * 5
    assert set(codes[5:]) == {429}


def test_spoofing_the_admins_address_cannot_lock_the_admin_out(world: World) -> None:
    admin_ip = "198.51.100.5"
    attacker = WebClient(TestClient(world.app))
    attacker.get("/login")
    for _ in range(6):
        _login(attacker, "wrong", f"{admin_ip}, {EDGE}")  # pretends to be the admin
    assert _login(attacker, PASSWORD, f"{admin_ip}, {EDGE}").status_code == 429

    admin = WebClient(TestClient(world.app))
    admin.get("/login")
    assert _login(admin, PASSWORD, admin_ip).status_code == 303


def test_client_key_uses_the_rightmost_trusted_hop() -> None:
    assert client_key([f"10.0.0.1, {EDGE}"], "10.1.1.1", 1) == EDGE
    assert client_key(["10.0.0.1", EDGE], "10.1.1.1", 1) == EDGE  # repeated header lines
    assert client_key([f"1.1.1.1, 10.0.0.1, {EDGE}"], "10.1.1.1", 2) == "10.0.0.1"
    assert client_key([EDGE], "10.1.1.1", 3) == EDGE  # fewer hops than configured
    assert client_key([], "10.1.1.1", 1) == "10.1.1.1"  # no proxy header (local)
    assert client_key([""], None, 1) == "unknown"
    assert client_key([f"{EDGE}"], "10.1.1.1", 0) == "10.1.1.1"  # no trusted proxy: ignore the header
    assert client_key(["not-an-ip"], "10.1.1.1", 1) == "unparseable-forwarded-for"
    assert client_key(["2001:db8::1"], None, 1) == "2001:db8::1"


def test_global_failure_budget_caps_distributed_guessing(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    web = WebClient(TestClient(world.app))
    web.get("/login")
    with caplog.at_level(logging.ERROR):
        codes = [_login(web, "wrong", f"192.0.2.{i}").status_code for i in range(1, 25)]
    assert codes[:20] == [401] * 20 and set(codes[20:]) == {429}
    assert caplog.text.count("ALERT: 20 failed dashboard logins") == 1  # alerted once, no values
    # Every client is blocked until the window passes, then logins work again.
    assert _login(web, PASSWORD, "192.0.2.200").status_code == 429
    world.clock.advance(minutes=15, seconds=1)
    assert _login(web, PASSWORD, "192.0.2.200").status_code == 303


def test_global_budget_unit() -> None:
    clock = FakeClock()
    limiter = LoginRateLimiter(clock, max_failures=5, global_max_failures=3)
    attempts = [limiter.begin_attempt(f"k{i}") for i in range(3)]
    assert all(a is not None for a in attempts)
    assert limiter.begin_attempt("k9") is None and limiter.is_blocked("k9")
    limiter.succeeded(attempts[0])  # a correct password un-counts its own attempt
    assert not limiter.is_blocked("k9")
    clock.advance(minutes=16)
    assert limiter.begin_attempt("k9") is not None


# --------------------------------------------------------------------------- #
# Finding 4: bounded Argon2 work on the public login
# --------------------------------------------------------------------------- #


class _CountingAuthenticator:
    def __init__(self) -> None:
        self.calls = 0

    def verify(self, password: str) -> bool:
        self.calls += 1
        return password == PASSWORD


def test_login_is_refused_without_verifying_when_all_slots_are_busy(world: World) -> None:
    app = create_app(services=world.services, settings=make_settings())
    counting = _CountingAuthenticator()
    app.state.authenticator = counting
    gate: VerificationGate = app.state.verification_gate
    web = WebClient(TestClient(app))
    web.get("/login")

    with gate.slot() as first, gate.slot() as second:  # two verifications in flight
        assert first and second
        busy = _login(web, PASSWORD)
    assert busy.status_code == 429 and "Try again in a moment" in busy.text
    assert counting.calls == 0  # no Argon2 work was started
    assert _login(web, PASSWORD).status_code == 303  # slots free again
    assert counting.calls == 1


def test_gate_releases_slots_and_limits_concurrency() -> None:
    gate = VerificationGate(max_concurrent=1)
    with gate.slot() as first:
        assert first
        with gate.slot() as second:
            assert not second
    with gate.slot() as again:
        assert again


def test_parallel_attempts_are_counted_before_verification() -> None:
    """Check-then-act is one step: of 40 simultaneous attempts from one address,
    exactly ``max_failures`` get to verify a password."""
    limiter = LoginRateLimiter(FakeClock(), max_failures=5)
    barrier = threading.Barrier(40)
    admitted: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        ok = limiter.begin_attempt("198.51.100.9") is not None
        with lock:
            admitted.append(ok)

    threads = [threading.Thread(target=attempt) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert admitted.count(True) == 5


def test_csrf_token_is_rotated_after_a_failed_login(anon: WebClient) -> None:
    anon.get("/login")
    first_token = anon.csrf
    failed = anon.client.post("/login", data={"csrf_token": first_token, "password": "wrong"})
    assert failed.status_code == 401
    replay = anon.client.post(
        "/login", data={"csrf_token": first_token, "password": "wrong"}, follow_redirects=False
    )
    assert replay.status_code == 403  # the token from the page cannot be reused for guessing


def test_new_password_hashes_use_explicit_owasp_parameters(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "synthetic-admin-password-1"
    assert hash_password.main([], prompt=lambda _: secret) == 0
    printed = capsys.readouterr().out.strip()
    params = extract_parameters(printed)
    assert (params.memory_cost, params.time_cost, params.parallelism) == (
        ARGON2_MEMORY_COST_KIB,
        ARGON2_TIME_COST,
        ARGON2_PARALLELISM,
    )
    assert params.memory_cost <= 19 * 1024
    assert PasswordHasher().verify(printed, secret)


def test_failure_paths_burn_the_same_argon2_cost_as_the_configured_hash() -> None:
    configured = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1).hash("synthetic")
    auth = PasswordAuthenticator(configured)
    assert auth._hasher.memory_cost == 64 and auth._hasher.time_cost == 1
    assert PasswordAuthenticator("")._hasher.memory_cost == ARGON2_MEMORY_COST_KIB
    assert PasswordAuthenticator("not-a-hash")._hasher.memory_cost == ARGON2_MEMORY_COST_KIB


# --------------------------------------------------------------------------- #
# Finding 7: server-side revocation
# --------------------------------------------------------------------------- #


def _copy_of(web: WebClient, world: World) -> TestClient:
    """A second browser holding a copy of ``web``'s session cookie (a stolen cookie)."""
    thief = TestClient(world.app)
    thief.cookies.set("dsw_session", web.client.cookies["dsw_session"])
    return thief


def test_logout_revokes_a_copied_cookie(client: WebClient, world: World) -> None:
    thief = _copy_of(client, world)
    assert thief.get("/", follow_redirects=False).status_code == 200  # the copy works...
    assert client.post("/logout").status_code == 303
    response = thief.get("/", follow_redirects=False)  # ...until the owner logs out
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert thief.post("/runs", data={}, follow_redirects=False).status_code == 401


def test_log_out_everywhere_ends_every_session(client: WebClient, world: World) -> None:
    other_device = WebClient(TestClient(world.app))
    assert other_device.login().status_code == 303
    thief = _copy_of(client, world)

    response = client.post("/logout/all")

    assert response.status_code == 303 and response.headers["location"] == "/login"
    for browser in (client.client, other_device.client, thief):
        assert browser.get("/", follow_redirects=False).status_code == 303
    assert world.store.latest_run() is None


def test_log_out_everywhere_button_is_on_every_page(client: WebClient) -> None:
    page = client.get("/").text
    assert 'action="/logout/all"' in page


def test_password_change_revokes_existing_sessions(client: WebClient, world: World) -> None:
    cookie = client.client.cookies["dsw_session"]
    new_hash = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("a-new-synthetic-password")
    # Redeploy with a new ADMIN_PASSWORD_HASH (same SESSION_SECRET, same database).
    app = create_app(services=world.services, settings=make_settings(admin_password_hash=new_hash))
    browser = TestClient(app)
    browser.cookies.set("dsw_session", cookie)
    assert browser.get("/", follow_redirects=False).status_code == 303
    # Re-deploying with the *same* hash keeps the session (it is the binding, not a restart).
    same = TestClient(create_app(services=world.services, settings=make_settings()))
    same.cookies.set("dsw_session", cookie)
    assert same.get("/", follow_redirects=False).status_code == 200


def test_is_authenticated_checks_the_password_binding() -> None:
    now_ts = int(NOW.timestamp())
    session = {"user": "admin", "login_at": now_ts, "pwv": "abc"}
    assert is_authenticated(session, NOW, 100, password_binding="abc")
    assert not is_authenticated(session, NOW, 100, password_binding="abd")
    assert not is_authenticated({"user": "admin", "login_at": now_ts}, NOW, 100, password_binding="abc")
    assert not is_authenticated({**session, "pwv": 7}, NOW, 100, password_binding="abc")


def test_session_without_a_server_record_is_rejected(client: WebClient, world: World) -> None:
    """A validly signed cookie whose sid is not in the database (e.g. revoked, or
    forged by someone holding SESSION_SECRET) is not a login."""
    assert world.services.sessions is not None
    world.services.sessions.end_all()
    response = client.client.get("/", follow_redirects=False)
    assert response.status_code == 303
    world.clock.advance(seconds=1)
    assert client.login().status_code == 303  # and a fresh login works again
    assert client.client.get("/", follow_redirects=False).status_code == 200


def test_login_page_does_not_loop_for_a_revoked_session(client: WebClient, world: World) -> None:
    assert world.services.sessions is not None
    world.services.sessions.end_all()
    page = client.client.get("/login", follow_redirects=False)
    assert page.status_code == 200 and 'type="password"' in page.text
