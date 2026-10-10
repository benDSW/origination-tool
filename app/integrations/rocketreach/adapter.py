"""RocketReach implementation of the :class:`~app.domain.ports.EmailFinder` port.

Maps raw RocketReach profiles to domain :class:`EmailLookupResult` values via
the guardrails. Daily caps and caching are deliberately NOT here: they belong
to the service layer, which knows about founders and the database.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

from pydantic import SecretStr

from app.domain.models import EmailLookupResult, LookupOutcome
from app.integrations.rocketreach import guardrails
from app.integrations.rocketreach.client import COMPLETE, FAILED, RocketReachClient

logger = logging.getLogger(__name__)

LOOKUP_CREDIT_TYPE = "standard_lookup"


class RocketReachAdapter:
    """``EmailFinder`` backed by RocketReach person lookup (by LinkedIn URL only)."""

    def __init__(self, client: RocketReachClient) -> None:
        self._client = client

    @classmethod
    def from_api_key(cls, api_key: str | SecretStr, **client_options: Any) -> RocketReachAdapter:
        """Convenience for wiring: build the client with production defaults."""
        return cls(RocketReachClient(api_key, **client_options))

    def __repr__(self) -> str:
        return "RocketReachAdapter()"

    def close(self) -> None:
        self._client.close()

    def find_email(
        self,
        *,
        linkedin_slug: str,
        full_name: str,
        company_domain: str | None,
    ) -> EmailLookupResult:
        """Look up by LinkedIn profile, then apply identity + quality guardrails.

        ``full_name`` is accepted for the port contract but never sent: searching
        by name is how the wrong person gets emailed.
        Raises IntegrationError only for transport/auth failures.
        """
        if not guardrails.is_valid_slug(linkedin_slug):
            return EmailLookupResult(outcome=LookupOutcome.REJECTED, reason="invalid LinkedIn slug")

        profile = self._client.lookup_person(guardrails.linkedin_url_for(linkedin_slug))
        result = self._evaluate(linkedin_slug, profile, company_domain)
        logger.info("rocketreach lookup outcome=%s", result.outcome.value)
        return result

    @staticmethod
    def _evaluate(
        linkedin_slug: str, profile: Mapping[str, Any] | None, company_domain: str | None
    ) -> EmailLookupResult:
        if profile is None:
            return EmailLookupResult(outcome=LookupOutcome.NOT_FOUND, reason="no RocketReach profile")
        status = profile.get("status")
        if status == FAILED:
            return EmailLookupResult(outcome=LookupOutcome.NOT_FOUND, reason="RocketReach lookup failed")
        if status != COMPLETE:
            return EmailLookupResult(outcome=LookupOutcome.ERROR, reason="lookup still pending")
        return guardrails.evaluate_profile(linkedin_slug, profile, company_domain)

    def remaining_credits(self) -> int | None:
        """Remaining standard-lookup credits; None when unlimited or unknown. Never raises."""
        try:
            return parse_remaining_credits(self._client.account())
        except Exception as exc:  # noqa: BLE001 - the dashboard must render even if RocketReach is down
            logger.warning("rocketreach credit check failed (%s)", type(exc).__name__)
            return None


def parse_remaining_credits(account: Mapping[str, Any]) -> int | None:
    """Extract ``remaining`` for standard lookups from an ``/account/`` payload."""
    usage = account.get("credit_usage")
    if not isinstance(usage, list):
        return None
    for entry in usage:
        if isinstance(entry, Mapping) and entry.get("credit_type") == LOOKUP_CREDIT_TYPE:
            return _to_int(entry.get("remaining"))
    return None


def _to_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return max(int(number), 0) if math.isfinite(number) else None
    return None
