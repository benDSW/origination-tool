"""Erasure suppression keys.

When a founder is forgotten (UK GDPR erasure, or an objection to contact), the
tool must not quietly re-create them from the next Evertrace signal. It keeps
only keyed hashes (HMAC-SHA256 with a server-side secret) of the identifiers a
new signal would carry: the LinkedIn slug, the Evertrace signal id and the
normalised email. Without the secret the hashes cannot be reversed by trying
likely slugs or emails, so the suppression list holds no usable personal data.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Iterable

from app.domain.templating import normalise_email

logger = logging.getLogger(__name__)

MIN_KEY_BYTES = 16
# Local development and tests only. Production refuses to start without
# SUPPRESSION_SECRET (see app.config.check_runtime_safety).
_DEVELOPMENT_KEY = b"origination-tool-development-suppression-key"


class SuppressionKeys:
    """Turns a founder's identifiers into opaque, comparable hashes."""

    def __init__(self, key: bytes) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError("suppression key is too short")
        self._key = key

    @classmethod
    def development(cls) -> SuppressionKeys:
        return cls(_DEVELOPMENT_KEY)

    def __repr__(self) -> str:  # never print the key
        return "SuppressionKeys(<hidden>)"

    def _hash(self, kind: str, value: str) -> str:
        message = f"{kind}:{value}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def for_identity(
        self,
        *,
        signal_id: str | None = None,
        linkedin_slug: str | None = None,
        emails: Iterable[str | None] = (),
    ) -> list[str]:
        """Hashes for every identifier given (blank or invalid values are skipped)."""
        hashes: list[str] = []
        if signal_id and signal_id.strip():
            hashes.append(self._hash("signal", signal_id.strip()))
        if linkedin_slug and linkedin_slug.strip():
            hashes.append(self._hash("slug", linkedin_slug.strip().strip("/").lower()))
        for raw in emails:
            email = normalise_email(raw) if raw else None
            if email:
                hashes.append(self._hash("email", email.lower()))
        return sorted(set(hashes))
