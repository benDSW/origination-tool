"""Per-search eligibility policy (location)."""

from __future__ import annotations

from app.domain.geography import is_inside_m25, is_uk
from app.domain.models import LocationPolicy, Signal


class EligibilityDecision:
    __slots__ = ("eligible", "reason")

    def __init__(self, eligible: bool, reason: str | None = None) -> None:
        self.eligible = eligible
        self.reason = reason

    def __bool__(self) -> bool:
        return self.eligible

    def __repr__(self) -> str:
        return f"EligibilityDecision(eligible={self.eligible}, reason={self.reason!r})"


def evaluate_location(signal: Signal, policy: LocationPolicy) -> EligibilityDecision:
    """Decide whether a founder's location fits the search's policy.

    * Every policy requires a UK country.
    * ``UK_OUTSIDE_LONDON`` also rejects London and inside-M25 places.
    * A UK founder with an unknown city is allowed (the Evertrace search itself
      already constrains location; we cannot prove they are in London).
    """
    if not is_uk(signal.country):
        return EligibilityDecision(False, f"Not UK-based (country: {signal.country or 'unknown'})")
    if policy is LocationPolicy.UK_OUTSIDE_LONDON and is_inside_m25(signal.city):
        return EligibilityDecision(False, f"London / inside M25 (city: {signal.city})")
    return EligibilityDecision(True)
