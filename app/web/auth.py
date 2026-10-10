"""Dashboard authentication: who may log in, how often they may try, and the session shape.

``Authenticator`` is the seam for future sign-in methods (Microsoft OAuth will be
another implementation); today there is a single admin password whose Argon2
hash lives in ``ADMIN_PASSWORD_HASH``.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import logging
import secrets
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError

from app.web.csrf import new_token

logger = logging.getLogger(__name__)

ADMIN_USER = "admin"
MAX_PASSWORD_LEN = 1024
MAX_NEXT_LEN = 2048
# OWASP's Argon2id baseline (19 MiB, t=2, p=1). Used for new hashes and for the
# dummy hash when no usable hash is configured; verification always uses the
# parameters stored in the configured hash itself.
ARGON2_TIME_COST = 2
ARGON2_MEMORY_COST_KIB = 19 * 1024
ARGON2_PARALLELISM = 1
MAX_CONCURRENT_VERIFICATIONS = 2


def recommended_hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=ARGON2_TIME_COST, memory_cost=ARGON2_MEMORY_COST_KIB, parallelism=ARGON2_PARALLELISM
    )


@runtime_checkable
class Authenticator(Protocol):
    def verify(self, password: str) -> bool:
        """True only if ``password`` is correct. Must never raise for a wrong password."""
        ...


class PasswordAuthenticator:
    """Checks the single admin password against an Argon2 hash.

    With no hash configured every login fails (and a warning is logged): the
    dashboard is never left open by a missing environment variable. Failure paths
    still run an Argon2 verification so timing does not reveal *why* a login failed.
    """

    def __init__(self, password_hash: str, *, hasher: PasswordHasher | None = None) -> None:
        self._hash = (password_hash or "").strip()
        self._hasher = hasher or _hasher_matching(self._hash)
        self._dummy: str | None = None
        if not self._hash:
            logger.warning("ADMIN_PASSWORD_HASH is not set: every dashboard login will fail")

    @property
    def configured(self) -> bool:
        return bool(self._hash)

    @property
    def session_binding(self) -> str:
        """A short, non-reversible tag of the configured hash, stored in each session.

        Changing ADMIN_PASSWORD_HASH changes the tag, which ends every existing
        session (a stolen cookie stops working once the password is changed). The
        hash embeds a random salt, so the tag reveals nothing about the password.
        """
        return hashlib.sha256(b"dsw-session-binding:" + self._hash.encode()).hexdigest()[:32]

    def verify(self, password: str) -> bool:
        if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LEN:
            self._burn()
            return False
        if not self._hash:
            logger.warning("Login attempted but ADMIN_PASSWORD_HASH is not set")
            self._burn(password)
            return False
        try:
            return self._hasher.verify(self._hash, password)
        except (VerificationError, InvalidHashError):
            return False

    def _burn(self, attempt: str | None = None) -> None:
        """Spend roughly the time a real verification would (constant-ish failure path)."""
        if self._dummy is None:
            self._dummy = self._hasher.hash(secrets.token_urlsafe(16))
        with contextlib.suppress(VerificationError, InvalidHashError):
            self._hasher.verify(self._dummy, attempt or "x")


def _hasher_matching(password_hash: str) -> PasswordHasher:
    """A hasher with the configured hash's own parameters, so the dummy verification
    on failure paths costs the same as a real one (no timing tell)."""
    if password_hash:
        with contextlib.suppress(InvalidHashError, ValueError):
            return PasswordHasher.from_parameters(extract_parameters(password_hash))
    return recommended_hasher()


class VerificationGate:
    """Caps concurrent password verifications.

    Argon2 is memory-hard by design (19-64 MiB per verification), and /login is
    public. Without a cap, a burst of parallel requests could exhaust memory or
    every threadpool worker. A request that cannot get a slot at once is refused
    (429) rather than queued.
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_VERIFICATIONS) -> None:
        self._slots = threading.BoundedSemaphore(max(1, max_concurrent))

    @contextlib.contextmanager
    def slot(self) -> Iterator[bool]:
        acquired = self._slots.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._slots.release()


@dataclass(frozen=True)
class LoginAttempt:
    """An attempt already counted as a failure; :meth:`LoginRateLimiter.succeeded` un-counts it."""

    key: str
    at: datetime


class LoginRateLimiter:
    """Failed-login limits, in memory:

    * at most ``max_failures`` per ``window`` per client key (IP), and
    * at most ``global_max_failures`` per ``window`` across *all* keys, so spoofed
      or distributed guessing is capped whatever the client address looks like.
      Hitting the global budget blocks every login until the window passes and
      logs an alert line.

    :meth:`begin_attempt` checks and counts in one locked step, so parallel
    requests cannot all pass the check before any failure is recorded.

    In-memory is enough for a single-instance dashboard; a restart resets it,
    which is acceptable next to the cost of Argon2 per guess.
    """

    def __init__(
        self,
        clock: Callable[[], datetime],
        *,
        max_failures: int = 5,
        window: timedelta = timedelta(minutes=15),
        max_keys: int = 10_000,
        global_max_failures: int = 20,
    ) -> None:
        self._clock = clock
        self._max = max_failures
        self._window = window
        self._max_keys = max_keys
        self._global_max = global_max_failures
        self._failures: dict[str, deque[datetime]] = {}
        self._global: deque[datetime] = deque()
        self._alerted = False
        self._lock = threading.Lock()

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return self._blocked(key)

    def begin_attempt(self, key: str) -> LoginAttempt | None:
        """None if blocked; otherwise count this attempt as a failure *now* and return it."""
        with self._lock:
            if self._blocked(key):
                return None
            at = self._clock()
            self._record(key, at)
            return LoginAttempt(key=key, at=at)

    def succeeded(self, attempt: LoginAttempt) -> None:
        """The password was right: clear the key's failures and un-count the attempt globally."""
        with self._lock:
            self._failures.pop(attempt.key, None)
            with contextlib.suppress(ValueError):
                self._global.remove(attempt.at)

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._record(key, self._clock())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _blocked(self, key: str) -> bool:
        if len(self._recent(key)) >= self._max:
            return True
        cutoff = self._clock() - self._window
        while self._global and self._global[0] <= cutoff:
            self._global.popleft()
        if len(self._global) >= self._global_max:
            if not self._alerted:
                self._alerted = True
                logger.error(
                    "ALERT: %d failed dashboard logins within %d minutes across all clients; "
                    "every login is blocked until the window passes",
                    len(self._global),
                    int(self._window.total_seconds() // 60),
                )
            return True
        self._alerted = False
        return False

    def _record(self, key: str, at: datetime) -> None:
        if key not in self._failures and len(self._failures) >= self._max_keys:
            self._prune()
            if len(self._failures) >= self._max_keys:  # still full: drop the oldest key
                self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(key, deque()).append(at)
        self._global.append(at)

    def _recent(self, key: str) -> deque[datetime]:
        attempts = self._failures.get(key)
        if attempts is None:
            return deque()
        cutoff = self._clock() - self._window
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            self._failures.pop(key, None)
        return attempts

    def _prune(self) -> None:
        for key in list(self._failures):
            self._recent(key)


# --------------------------------------------------------------------------- #
# Session helpers. A logged-in session holds only
# {"user", "csrf", "login_at", "sid", "pwv"}: "sid" names the server-side session
# record (revocable), "pwv" binds the session to the current password hash.
# --------------------------------------------------------------------------- #


def start_session(
    session: MutableMapping[str, Any],
    now: datetime,
    *,
    sid: str | None = None,
    password_binding: str | None = None,
) -> None:
    """Rotate: drop everything from the anonymous session, then mark it logged in."""
    session.clear()
    session["user"] = ADMIN_USER
    session["csrf"] = new_token()
    session["login_at"] = int(now.timestamp())
    if sid is not None:
        session["sid"] = sid
    if password_binding is not None:
        session["pwv"] = password_binding


def end_session(session: MutableMapping[str, Any]) -> None:
    session.clear()


def rotate_csrf(session: MutableMapping[str, Any]) -> None:
    """New anonymous CSRF token (after a failed login), so one token fetched from
    the login page cannot be replayed for a whole guessing campaign."""
    session["csrf"] = new_token()


def is_authenticated(
    session: MutableMapping[str, Any],
    now: datetime,
    max_age_seconds: int,
    *,
    password_binding: str | None = None,
) -> bool:
    """Logged in, and the login is younger than the absolute session lifetime.

    The signed cookie is re-issued on every response (a sliding expiry), so the
    absolute 12-hour limit is enforced here from ``login_at``. With
    ``password_binding``, the session must also have been issued under the current
    password hash. (The server-side ``sid`` check is :func:`session_is_live`.)
    """
    if session.get("user") != ADMIN_USER:
        return False
    login_at = session.get("login_at")
    if not isinstance(login_at, int) or isinstance(login_at, bool):
        return False
    age = now.timestamp() - login_at
    if not -60 <= age <= max_age_seconds:
        return False
    if password_binding is not None:
        bound = session.get("pwv")
        if not isinstance(bound, str) or not hmac.compare_digest(bound.encode(), password_binding.encode()):
            return False
    return True


def session_is_live(state: Any, session: MutableMapping[str, Any]) -> bool:
    """The full check for a request: a valid, unexpired login bound to the current
    password, whose server-side session record still exists (not logged out or
    revoked). Blocking (one DB read): call it from a worker thread.

    ``state`` is the app's ``request.app.state``.
    """
    if not is_authenticated(
        session,
        state.clock(),
        state.config.session_max_age_seconds,
        password_binding=getattr(state, "session_binding", None),
    ):
        return False
    sessions = getattr(getattr(state, "services", None), "sessions", None)
    return sessions is not None and bool(sessions.is_active(session.get("sid")))


# --------------------------------------------------------------------------- #
# Client address for rate limiting
# --------------------------------------------------------------------------- #


def client_key(forwarded_for: Iterable[str], peer: str | None, trusted_hops: int) -> str:
    """The rate-limit key: the client address as seen by our own edge proxy.

    Each proxy *appends* the address it received the request from to
    X-Forwarded-For, so the entry ``trusted_hops`` from the right was written by
    our edge and cannot be forged. The leftmost entry (which uvicorn's
    ``--forwarded-allow-ips '*'`` copies into ``request.client``) is whatever the
    client sent, so it is never used. With ``trusted_hops=0`` (no proxy) the TCP
    peer is the key.
    """
    hops = [h.strip() for value in forwarded_for for h in value.split(",") if h.strip()]
    if trusted_hops <= 0 or not hops:
        return peer or "unknown"
    candidate = hops[-trusted_hops] if len(hops) >= trusted_hops else hops[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        # Our edge never writes junk; if it somehow does, share one bucket (fail closed).
        return "unparseable-forwarded-for"


def safe_next(value: str | None, default: str = "/") -> str:
    """Only local paths ("/drafts", "/outreach/3?x=1") are allowed as redirect targets.

    Rejects absolute URLs, scheme-relative ``//host``, backslash tricks and
    control characters, so a crafted link cannot bounce the user off-site.
    """
    if not value or not isinstance(value, str) or len(value) > MAX_NEXT_LEN:
        return default
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    return value
