"""Guardrails that stop us emailing the wrong person.

Everything here is a pure function over already-decoded JSON so it can be
unit-tested exhaustively without HTTP. The rules (see plan, "RocketReach
guardrails"):

1. Look up ONLY by a LinkedIn URL built from a validated slug; never by name.
2. The returned profile must point at the same LinkedIn slug we asked for.
3. Only A/A- graded or SMTP-valid emails are candidates; SMTP-invalid and
   F-graded addresses are always dropped; every address must survive the
   domain's header-injection-safe ``normalise_email``.
4. Prefer a professional address on the company's own domain, then any
   professional address, then a personal one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from app.domain.models import EmailLookupResult, LookupOutcome
from app.domain.templating import normalise_email

SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.%-]{1,99}$")
LINKEDIN_PROFILE_BASE = "https://www.linkedin.com/in/"

ACCEPTED_GRADES: frozenset[str] = frozenset({"A", "A-"})
REJECTED_GRADES: frozenset[str] = frozenset({"F"})
_GRADE_RANK: dict[str, int] = {"A": 0, "A-": 1}

NO_VERIFIED_EMAIL = "no verified / A-grade email"


# --------------------------------------------------------------------------- #
# 1. Slug validation and URL construction
# --------------------------------------------------------------------------- #


def is_valid_slug(slug: str | None) -> bool:
    """True when ``slug`` is a bare LinkedIn handle that is safe to put in a URL."""
    return isinstance(slug, str) and SLUG_PATTERN.fullmatch(slug) is not None


def linkedin_url_for(slug: str) -> str:
    """Canonical profile URL for a validated slug. Raises ValueError otherwise."""
    if not is_valid_slug(slug):
        raise ValueError("invalid LinkedIn slug")
    return f"{LINKEDIN_PROFILE_BASE}{slug}"


def _canonical_slug(slug: str) -> str:
    """Comparison key: url-decoded and case-folded."""
    return unquote(slug).strip().casefold()


# --------------------------------------------------------------------------- #
# 2. Identity check
# --------------------------------------------------------------------------- #


def _is_linkedin_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def slug_from_linkedin_url(url: object) -> str | None:
    """Extract the canonical slug from a LinkedIn profile URL, or None if it isn't one.

    Accepts any linkedin.com host/subdomain (``uk.linkedin.com``), an optional
    scheme, a trailing slash, and ignores query string / fragment. The path
    must be exactly ``/in/<slug>``.
    """
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not _is_linkedin_host(host):
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) != 2 or segments[0].lower() != "in":
        return None
    slug = _canonical_slug(segments[1])
    return slug or None


def identity_rejection_reason(requested_slug: str, profile_linkedin_url: object) -> str | None:
    """None when the profile is the person we asked for, else a short reason."""
    if not isinstance(profile_linkedin_url, str) or not profile_linkedin_url.strip():
        return "profile has no LinkedIn URL to verify identity"
    returned = slug_from_linkedin_url(profile_linkedin_url)
    if returned is None:
        return "profile LinkedIn URL is not a linkedin.com/in/ profile"
    if returned != _canonical_slug(requested_slug):
        return "profile LinkedIn URL does not match the requested founder"
    return None


# --------------------------------------------------------------------------- #
# 3 + 4. Email quality and preference
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderEmail:
    """One email entry from a RocketReach profile, with every field optional."""

    email: str | None
    smtp_valid: str | None = None
    type: str | None = None
    grade: str | None = None


def _norm_str(value: object, *, upper: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text.upper() if upper else text.lower()


def parse_emails(raw: object) -> list[ProviderEmail]:
    """Tolerantly turn the profile's ``emails`` value into ProviderEmail entries."""
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return []
    parsed: list[ProviderEmail] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        email = item.get("email")
        parsed.append(
            ProviderEmail(
                email=email if isinstance(email, str) else None,
                smtp_valid=_norm_str(item.get("smtp_valid")),
                type=_norm_str(item.get("type")),
                grade=_norm_str(item.get("grade"), upper=True),
            )
        )
    return parsed


def is_acceptable_quality(entry: ProviderEmail) -> bool:
    """Grade A/A- or SMTP-valid, and never SMTP-invalid or grade F."""
    if entry.smtp_valid == "invalid" or entry.grade in REJECTED_GRADES:
        return False
    return entry.grade in ACCEPTED_GRADES or entry.smtp_valid == "valid"


def normalise_domain(domain: str | None) -> str | None:
    """Bare lower-case domain without a leading ``www.``/``@`` (None if empty)."""
    if not domain:
        return None
    text = domain.strip().lower().lstrip("@").rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    return text or None


def domain_matches(email: str, company_domain: str | None) -> bool:
    """True when the address is on ``company_domain`` or one of its subdomains."""
    target = normalise_domain(company_domain)
    if target is None or "@" not in email:
        return False
    email_domain = email.rsplit("@", 1)[1].lower()
    return email_domain == target or email_domain.endswith("." + target)


@dataclass(frozen=True, slots=True)
class _Candidate:
    email: str
    grade: str | None
    tier: int
    reason: str
    grade_rank: int
    position: int


def _tier(entry: ProviderEmail, email: str, company_domain: str | None) -> tuple[int, str]:
    on_domain = domain_matches(email, company_domain)
    if entry.type == "professional" and on_domain:
        return 0, "professional email on the company domain"
    if entry.type is None and on_domain:
        return 1, "email on the company domain"
    if entry.type == "professional":
        return 2, "professional email"
    if entry.type == "personal":
        return 4, "personal email"
    return 3, "email of unknown type"


def select_email(emails: Sequence[ProviderEmail], company_domain: str | None) -> EmailLookupResult:
    """Pick the best acceptable address, or NOT_FOUND when none survives the filters."""
    candidates: list[_Candidate] = []
    seen: set[str] = set()
    for position, entry in enumerate(emails):
        if not is_acceptable_quality(entry):
            continue
        email = normalise_email(entry.email)
        if email is None or email.lower() in seen:
            continue
        seen.add(email.lower())
        tier, reason = _tier(entry, email, company_domain)
        candidates.append(
            _Candidate(
                email=email,
                grade=entry.grade,
                tier=tier,
                reason=reason,
                grade_rank=_GRADE_RANK.get(entry.grade or "", 2),
                position=position,
            )
        )
    if not candidates:
        return EmailLookupResult(outcome=LookupOutcome.NOT_FOUND, reason=NO_VERIFIED_EMAIL)
    best = min(candidates, key=lambda c: (c.tier, c.grade_rank, c.position))
    return EmailLookupResult(
        outcome=LookupOutcome.FOUND, email=best.email, grade=best.grade, reason=best.reason
    )


def evaluate_profile(
    requested_slug: str, profile: Mapping[str, Any], company_domain: str | None
) -> EmailLookupResult:
    """Apply the identity check, then the email quality/preference rules, to a complete profile."""
    reason = identity_rejection_reason(requested_slug, profile.get("linkedin_url"))
    if reason is not None:
        return EmailLookupResult(outcome=LookupOutcome.REJECTED, reason=reason)
    return select_email(parse_emails(profile.get("emails")), company_domain)
