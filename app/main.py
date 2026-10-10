"""Dashboard entry point: ``uvicorn app.main:app``.

This is a composition root (like ``app.container``): it turns ``Settings`` into
the web layer's :class:`WebConfig`, picks the authenticator, and tells the app
how to build its services. Nothing here touches the network or reads secrets at
import time: ``app`` is created on first access (which is what uvicorn does), and
the services are built in the app's lifespan, at startup.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from app.config import Settings, check_runtime_safety, describe_security_mode, get_settings
from app.container import build_services
from app.domain.models import RunRecord
from app.observability import configure_logging
from app.services import Services
from app.web import PasswordAuthenticator, WebConfig, create_web_app, latest_run_only, resolve_session_secret

logger = logging.getLogger(__name__)


def create_app(services: Services | None = None, settings: Settings | None = None) -> FastAPI:
    """Build the dashboard.

    ``services`` given (tests): used as-is and owned by the caller. Otherwise
    they are built from ``settings`` when the app starts and closed when it stops.
    In production this refuses to start without a strong ``SESSION_SECRET``.
    """
    settings = settings or get_settings()
    config = WebConfig(
        session_secret=resolve_session_secret(
            settings.session_secret.get_secret_value(), production=settings.is_production
        ),
        production=settings.is_production,
        session_max_age_seconds=settings.session_max_age_seconds,
        trusted_proxy_hops=settings.trusted_proxy_hops,
    )
    authenticator = PasswordAuthenticator(settings.admin_password_hash.get_secret_value())
    return create_web_app(
        config,
        authenticator,
        services=services,
        services_factory=None if services is not None else (lambda: build_services(settings)),
        run_history=run_history,
    )


def run_history(services: Services, limit: int) -> list[RunRecord]:
    """Newest-first run history for the Runs page.

    The services bundle has no public run-history method yet, so this composition
    root (which may know concrete wiring) reads it from the origination service's
    store. Prefer a public ``list_runs`` as soon as services offer one.
    """
    lister = getattr(services.origination, "list_runs", None)
    if callable(lister):
        return list(lister(limit))
    store = getattr(services.origination, "_store", None)
    store_lister = getattr(store, "list_runs", None)
    if callable(store_lister):
        return list(store_lister(limit))
    return latest_run_only(services, limit)


_app: FastAPI | None = None


def _default_app() -> FastAPI:
    """The production app: settings from the environment, redacting logs first."""
    settings = get_settings()
    configure_logging(settings.secret_values())
    check_runtime_safety(settings)  # refuse a deployment that would run fail-open
    logger.info("Dashboard security mode: %s", describe_security_mode(settings))
    return create_app(settings=settings)


def __getattr__(name: str) -> Any:
    """``app.main.app`` is built on first access, so importing this module (tests,
    tooling) never reads the environment or ``.env``."""
    global _app
    if name == "app":
        if _app is None:
            _app = _default_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
