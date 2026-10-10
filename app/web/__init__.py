"""Dashboard layer: FastAPI routes, auth, CSRF, security headers and templates.

It talks only to ``app.services`` and ``app.domain`` (enforced by
``.importlinter``): never to an adapter, the database or ``app.config``. The
composition root (``app.main``) builds :class:`WebConfig` from settings and calls
:func:`create_web_app`.
"""

from app.web.auth import Authenticator, LoginRateLimiter, PasswordAuthenticator, VerificationGate
from app.web.config import InsecureConfigError, WebConfig, resolve_session_secret
from app.web.deps import RunHistory, latest_run_only
from app.web.factory import create_web_app

__all__ = [
    "Authenticator",
    "InsecureConfigError",
    "LoginRateLimiter",
    "PasswordAuthenticator",
    "RunHistory",
    "VerificationGate",
    "WebConfig",
    "create_web_app",
    "latest_run_only",
    "resolve_session_secret",
]
