"""CSRF protection: a synchronizer token kept in the signed session.

Every unsafe request (POST/PUT/PATCH/DELETE) must echo the session's token in a
form field (``csrf_token``) or, for ``fetch``, the ``X-CSRF-Token`` header. The
check is installed as a *global* FastAPI dependency, so a newly added route is
protected without anyone remembering to opt in.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import MutableMapping
from typing import Any

from fastapi import HTTPException, Request

CSRF_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")
_MAX_TOKEN_LEN = 200


def new_token() -> str:
    return secrets.token_urlsafe(32)


def ensure_token(session: MutableMapping[str, Any]) -> str:
    """The session's token, creating one for an anonymous visitor (the login form)."""
    token = session.get("csrf")
    if not isinstance(token, str) or not token:
        token = new_token()
        session["csrf"] = token
    return token


def tokens_match(expected: object, supplied: object) -> bool:
    if not isinstance(expected, str) or not isinstance(supplied, str):
        return False
    if not expected or not supplied or len(supplied) > _MAX_TOKEN_LEN:
        return False
    return hmac.compare_digest(expected.encode(), supplied.encode())


async def csrf_protect(request: Request) -> None:
    """FastAPI dependency: 403 unless an unsafe request carries the session's token."""
    if request.method.upper() in SAFE_METHODS:
        return
    expected = request.session.get("csrf")
    supplied: object = request.headers.get(CSRF_HEADER)
    if supplied is None:
        content_type = request.headers.get("content-type", "").lower()
        if content_type.startswith(_FORM_TYPES):
            form = await request.form()  # cached on the request; the route reuses it
            supplied = form.get(CSRF_FIELD)
    if not tokens_match(expected, supplied):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
