"""Dashboard routers. Every router listed here sits behind the auth gate and the
global CSRF check (see ``app.web.factory``); only /login, /healthz and /static
are public."""

from fastapi import APIRouter

from app.web.routes import auth, health, outreach, overview, searches, template

ALL_ROUTERS: tuple[APIRouter, ...] = (
    health.router,
    auth.router,
    overview.router,
    outreach.router,
    searches.router,
    template.router,
)

__all__ = ["ALL_ROUTERS"]
