"""Login / logout."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Form, Query, Request
from starlette.responses import RedirectResponse, Response

from app.web.auth import (
    MAX_NEXT_LEN,
    MAX_PASSWORD_LEN,
    Authenticator,
    LoginRateLimiter,
    VerificationGate,
    client_key,
    end_session,
    rotate_csrf,
    safe_next,
    session_is_live,
    start_session,
)
from app.web.rendering import redirect, render

logger = logging.getLogger(__name__)
router = APIRouter()

TOO_MANY = "Too many failed attempts. Try again in 15 minutes."
BUSY = "Too many login attempts right now. Try again in a moment."


def _client_key(request: Request) -> str:
    """Rate-limit key from the rightmost trusted X-Forwarded-For hop (see
    :func:`app.web.auth.client_key`), never the client-controlled leftmost one."""
    return client_key(
        request.headers.getlist("x-forwarded-for"),
        request.client.host if request.client else None,
        request.app.state.config.trusted_proxy_hops,
    )


def _sessions(request: Request) -> Any:
    return getattr(getattr(request.app.state, "services", None), "sessions", None)


@router.get("/login")
def login_page(
    request: Request, next_url: Annotated[str, Query(alias="next", max_length=MAX_NEXT_LEN)] = "/"
) -> Response:
    target = safe_next(next_url)
    if session_is_live(request.app.state, request.session):
        return RedirectResponse(target, status_code=303)
    return render(request, "login.html", {"next": target, "error": None})


@router.post("/login")
def login(
    request: Request,
    password: Annotated[str, Form(max_length=MAX_PASSWORD_LEN)],
    next_url: Annotated[str, Form(alias="next", max_length=MAX_NEXT_LEN)] = "/",
) -> Response:
    """Sync on purpose: Argon2 verification is CPU-bound and runs in the threadpool.

    Order matters: a bounded number of verifications may run at once (memory),
    and the attempt is counted *before* verifying, atomically with the limit check.
    """
    state = request.app.state
    limiter: LoginRateLimiter = state.rate_limiter
    gate: VerificationGate = state.verification_gate
    authenticator: Authenticator = state.authenticator
    target = safe_next(next_url)
    sessions = _sessions(request)
    if sessions is None:
        return render(
            request,
            "login.html",
            {"next": target, "error": "The dashboard is still starting up."},
            status_code=503,
        )
    with gate.slot() as acquired:
        if not acquired:
            logger.warning("Dashboard login refused: too many concurrent verifications")
            return render(request, "login.html", {"next": target, "error": BUSY}, status_code=429)
        attempt = limiter.begin_attempt(_client_key(request))
        if attempt is None:
            logger.warning("Dashboard login rate limit hit")
            return render(request, "login.html", {"next": target, "error": TOO_MANY}, status_code=429)
        verified = authenticator.verify(password)
    if not verified:
        logger.warning("Failed dashboard login")
        rotate_csrf(request.session)
        return render(
            request, "login.html", {"next": target, "error": "Incorrect password."}, status_code=401
        )
    limiter.succeeded(attempt)
    sid = sessions.start(state.config.session_max_age_seconds)
    start_session(request.session, state.clock(), sid=sid, password_binding=state.session_binding)
    logger.info("Dashboard login")
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    sessions = _sessions(request)
    if sessions is not None:
        sessions.end(request.session.get("sid"))  # the cookie stops working everywhere
    end_session(request.session)
    return redirect(request, "/login", "You have been logged out.", "info")


@router.post("/logout/all")
def logout_everywhere(request: Request) -> Response:
    """Revoke every dashboard session, including this one (suspected compromise)."""
    sessions = _sessions(request)
    revoked = sessions.end_all() if sessions is not None else 0
    end_session(request.session)
    logger.warning("All dashboard sessions revoked (%d)", revoked)
    return redirect(request, "/login", "Every session has been logged out, including this one.", "info")
