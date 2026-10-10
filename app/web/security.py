"""ASGI middleware: security headers, request-size limit and the authentication gate.

These are plain ASGI wrappers (not ``BaseHTTPMiddleware``) so they see every
response, including static files and error pages, and never buffer bodies.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.web.auth import safe_next, session_is_live

logger = logging.getLogger(__name__)

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"
HSTS = "max-age=31536000; includeSubDomains"

PUBLIC_PATHS = frozenset({"/login", "/healthz"})
STATIC_PREFIX = "/static/"

_INTERNAL_ERROR_PAGE = (
    "<!doctype html><html lang=en><head><meta charset=utf-8><title>Error</title>"
    '<link rel="stylesheet" href="/static/app.css"></head><body><main class="narrow">'
    "<h1>Something went wrong</h1><p>The error has been logged. "
    '<a href="/">Back to the overview</a></p></main></body></html>'
)
_UNAUTHORISED_PAGE = (
    "<!doctype html><html lang=en><head><meta charset=utf-8><title>Log in</title>"
    '<link rel="stylesheet" href="/static/app.css"></head><body><main class="narrow">'
    '<h1>Please log in</h1><p>Your session has expired or you are not logged in. <a href="/login">Log in</a>'
    "</p></main></body></html>"
)


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(STATIC_PREFIX)


class SecurityHeadersMiddleware:
    """Adds the security headers to every HTTP response and turns any unhandled
    exception into a bare 500 page that never echoes the exception text."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_static = scope.get("path", "").startswith(STATIC_PREFIX)
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
                headers["X-Frame-Options"] = "DENY"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Permissions-Policy"] = PERMISSIONS_POLICY
                headers["Cross-Origin-Opener-Policy"] = "same-origin"
                headers["Cross-Origin-Resource-Policy"] = "same-origin"
                if self.hsts:
                    headers["Strict-Transport-Security"] = HSTS
                if not is_static:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as exc:
            # Logged by type only at error level; the redacting log filter guards the traceback.
            logger.error(
                "Unhandled %s on %s %s",
                type(exc).__name__,
                scope.get("method"),
                scope.get("path"),
                exc_info=exc,
            )
            if started:
                raise
            await HTMLResponse(_INTERNAL_ERROR_PAGE, status_code=500)(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """Rejects request bodies over ``max_bytes`` (declared or streamed) with 413."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = self.max_bytes + 1
                if declared > self.max_bytes:
                    await PlainTextResponse("Request too large", status_code=413)(scope, receive, send)
                    return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await PlainTextResponse("Request too large", status_code=413)(scope, receive, send)


class _TooLarge(Exception):
    pass


class AuthGateMiddleware:
    """Fail-closed authentication: every path except /login, /healthz and /static/*
    needs a live logged-in session (see :func:`app.web.auth.session_is_live`: an
    unexpired login, bound to the current password, whose server-side record has
    not been logged out or revoked). New routes are protected automatically.

    Anonymous GET/HEAD -> 303 to /login?next=...; any other method -> 401.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_public_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        session = scope.get("session")
        state = scope["app"].state
        if session is not None and await run_in_threadpool(session_is_live, state, session):
            await self.app(scope, receive, send)
            return
        if session is not None and session.get("user"):
            session.clear()  # expired login: drop it entirely
        if scope.get("method", "GET") in ("GET", "HEAD"):
            target = scope.get("path", "/")
            query = scope.get("query_string", b"").decode("latin-1")
            if query:
                target = f"{target}?{query}"
            target = safe_next(target)
            location = "/login" if target == "/" else "/login?next=" + quote(target, safe="")
            response: HTMLResponse | RedirectResponse = RedirectResponse(location, status_code=303)
        else:
            response = HTMLResponse(_UNAUTHORISED_PAGE, status_code=401)
        await response(scope, receive, send)
