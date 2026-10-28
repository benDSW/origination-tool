"""Builds the dashboard ASGI app from already-made parts.

Everything the web layer needs is passed in (services, authenticator, clock,
config), so tests can wire fakes and ``app.main`` stays the only place that
knows about ``Settings`` and the container.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from app.services import Services
from app.services.clock import utc_now
from app.web.auth import Authenticator, LoginRateLimiter, VerificationGate
from app.web.background import tracking_loop
from app.web.config import WebConfig
from app.web.csrf import csrf_protect
from app.web.deps import Clock, RunHistory, latest_run_only
from app.web.errors import install_error_handlers
from app.web.flash import FlashStore
from app.web.routes import ALL_ROUTERS
from app.web.security import AuthGateMiddleware, BodySizeLimitMiddleware, SecurityHeadersMiddleware

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Windows can map .js to text/plain via the registry; with nosniff the browser
# would then refuse to run app.js. Pin the types we serve.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def create_web_app(
    config: WebConfig,
    authenticator: Authenticator,
    *,
    services: Services | None = None,
    services_factory: Callable[[], Services] | None = None,
    run_history: RunHistory | None = None,
    clock: Clock | None = None,
    rate_limiter: LoginRateLimiter | None = None,
    verification_gate: VerificationGate | None = None,
) -> FastAPI:
    """Assemble the FastAPI app.

    Either pass ready ``services`` (tests; the caller owns and closes them) or a
    ``services_factory`` that the lifespan calls at startup and closes at
    shutdown (production; nothing touches the network at import time).
    """
    clock = clock or (services.clock if services is not None else utc_now)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        built: Services | None = None
        if app.state.services is None and services_factory is not None:
            built = await run_in_threadpool(services_factory)
            app.state.services = built
        tracker: asyncio.Task[None] | None = None
        if config.tracking_interval_seconds > 0:
            tracker = asyncio.create_task(
                tracking_loop(lambda: app.state.services, config.tracking_interval_seconds)
            )
        try:
            yield
        finally:
            if tracker is not None:
                tracker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tracker
            if built is not None:
                built.close()
                app.state.services = None

    app = FastAPI(
        title="DSW Origination",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        dependencies=[Depends(csrf_protect)],  # global: every unsafe request needs the token
    )
    app.state.services = services
    app.state.authenticator = authenticator
    app.state.rate_limiter = rate_limiter or LoginRateLimiter(clock)
    app.state.verification_gate = verification_gate or VerificationGate()
    # Ties sessions to the current password hash (None: an authenticator without one).
    app.state.session_binding = getattr(authenticator, "session_binding", None)
    app.state.clock = clock
    app.state.config = config
    app.state.flash = FlashStore(config.session_secret, secure=config.https_only)
    app.state.run_history = run_history or latest_run_only

    for router in ALL_ROUTERS:
        app.include_router(router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    install_error_handlers(app)

    # Last added = outermost. Order (outside -> in): headers, size limit, session, auth gate.
    app.add_middleware(AuthGateMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret,
        session_cookie=config.session_cookie,
        max_age=config.session_max_age_seconds,
        same_site="lax",
        https_only=config.https_only,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=config.max_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware, hsts=config.production)
    return app
