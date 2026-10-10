"""Web-layer configuration.

The dashboard must not import ``app.config`` (it only knows services and the
domain), so the composition root (``app.main``) translates ``Settings`` into this
small, explicit value object.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MIN_SESSION_SECRET_LEN = 32
DEFAULT_SESSION_MAX_AGE = 12 * 60 * 60
DEFAULT_MAX_BODY_BYTES = 256 * 1024


class InsecureConfigError(RuntimeError):
    """The dashboard refuses to start with this configuration (production only)."""


@dataclass(frozen=True)
class WebConfig:
    session_secret: str
    production: bool = False
    session_max_age_seconds: int = DEFAULT_SESSION_MAX_AGE
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    session_cookie: str = "dsw_session"
    # Proxies in front of the app that append to X-Forwarded-For (Railway's edge: 1).
    # The login rate limit keys on the entry this many hops from the right.
    trusted_proxy_hops: int = 1

    @property
    def https_only(self) -> bool:
        """Secure cookies + HSTS only in production (local dev runs on plain http)."""
        return self.production

    def __repr__(self) -> str:  # never print the secret
        return (
            f"WebConfig(production={self.production}, session_max_age_seconds={self.session_max_age_seconds})"
        )


def resolve_session_secret(configured: str, *, production: bool) -> str:
    """Return the session-signing secret to use.

    Production refuses to start without a strong secret (a guessable secret lets
    anyone forge an admin session). Development and tests get an ephemeral random
    secret instead, so sessions simply do not survive a restart.
    """
    configured = configured or ""
    if len(configured) >= MIN_SESSION_SECRET_LEN:
        return configured
    if production:
        raise InsecureConfigError(
            f"SESSION_SECRET must be set to at least {MIN_SESSION_SECRET_LEN} characters in production"
        )
    logger.warning(
        "SESSION_SECRET is missing or shorter than %d characters: using an ephemeral random secret "
        "(sessions will not survive a restart)",
        MIN_SESSION_SECRET_LEN,
    )
    return secrets.token_urlsafe(48)
