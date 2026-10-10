"""Server-side dashboard sessions.

The session cookie is signed, not stored, so on its own it stays valid until it
expires: logging out in one browser would not end a copy of the cookie taken
earlier. Each login therefore registers a random session id here, and every
request checks it is still active. Logout deletes it; "log out everywhere"
deletes them all. Only a SHA-256 of the id is stored, so a database dump cannot
be turned back into a working cookie.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from app.domain.ports import Store
from app.services.clock import Clock, utc_now

MAX_SID_LEN = 200


def _digest(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def _valid(sid: object) -> bool:
    return isinstance(sid, str) and 0 < len(sid) <= MAX_SID_LEN


class SessionService:
    def __init__(self, store: Store, clock: Clock = utc_now) -> None:
        self._store = store
        self._clock = clock

    def start(self, max_age_seconds: int) -> str:
        """Register a new session and return its id (to be kept in the signed cookie)."""
        sid = secrets.token_urlsafe(32)
        now = self._clock()
        self._store.add_admin_session(
            _digest(sid), created_at=now, expires_at=now + timedelta(seconds=max(1, max_age_seconds))
        )
        return sid

    def is_active(self, sid: object) -> bool:
        if not _valid(sid):
            return False
        return self._store.admin_session_active(_digest(str(sid)), now=self._clock())

    def end(self, sid: object) -> None:
        if _valid(sid):
            self._store.delete_admin_session(_digest(str(sid)))

    def end_all(self) -> int:
        """Revoke every session (suspected compromise). Returns how many were live."""
        return self._store.delete_all_admin_sessions()
