"""R9 guardrails: identity (slug) check and email quality/preference. Pure functions."""

from __future__ import annotations

import pytest

from app.domain.models import LookupOutcome
from app.integrations.rocketreach.guardrails import (
    NO_VERIFIED_EMAIL,
    ProviderEmail,
    domain_matches,
    evaluate_profile,
    identity_rejection_reason,
    is_acceptable_quality,
    is_valid_slug,
    linkedin_url_for,
    normalise_domain,
    parse_emails,
    select_email,
    slug_from_linkedin_url,
)

# --------------------------------------------------------------------------- #
# Slug validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "slug",
    ["alex-testfounder-1", "ab", "Alex.Test_Founder", "jos%C3%A9-test", "a" * 100, "1abc"],
)
def test_valid_slugs(slug: str) -> None:
    assert is_valid_slug(slug)
    assert linkedin_url_for(slug) == f"https://www.linkedin.com/in/{slug}"


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "a",
        "a" * 101,
        "-leading-dash",
        ".leading-dot",
        "has space",
        "slash/inside",
        "../etc",
        "q?x=1",
        "frag#x",
        "amp&x=1",
        "line\nbreak",
        "https://www.linkedin.com/in/alex",
        "alex\r\nApi-Key: x",
        None,
        123,
    ],
)
def test_invalid_slugs(slug: object) -> None:
    assert not is_valid_slug(slug)  # type: ignore[arg-type]


def test_linkedin_url_for_rejects_invalid_slug() -> None:
    with pytest.raises(ValueError):
        linkedin_url_for("bad slug")


# --------------------------------------------------------------------------- #
# Identity check
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/alex-testfounder-1",
        "https://www.linkedin.com/in/alex-testfounder-1/",
        "http://www.linkedin.com/in/alex-testfounder-1",
        "https://linkedin.com/in/alex-testfounder-1",
        "https://uk.linkedin.com/in/alex-testfounder-1",
        "https://UK.LinkedIn.com/in/Alex-TestFounder-1",
        "www.linkedin.com/in/alex-testfounder-1",
        "linkedin.com/in/alex-testfounder-1/",
        "https://www.linkedin.com/in/alex-testfounder-1?trk=public_profile",
        "https://www.linkedin.com/in/alex-testfounder-1/#about",
        "https://www.linkedin.com/IN/alex-testfounder-1",
        "  https://www.linkedin.com/in/alex-testfounder-1  ",
        "https://www.linkedin.com/in/alex%2Dtestfounder%2D1",
    ],
)
def test_identity_accepts_same_slug_variants(url: str) -> None:
    assert identity_rejection_reason("alex-testfounder-1", url) is None


def test_identity_url_decoding_both_sides() -> None:
    assert identity_rejection_reason("jos%C3%A9-test", "https://www.linkedin.com/in/jos%C3%A9-test/") is None
    assert identity_rejection_reason("jos%C3%A9-test", "https://www.linkedin.com/in/josé-test") is None
    assert identity_rejection_reason("JOS%c3%a9-Test", "https://uk.linkedin.com/in/jos%C3%A9-test") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/alex-testfounder-2",  # a different person
        "https://www.linkedin.com/in/alex-testfounder",
        "https://www.linkedin.com/in/alex-testfounder-1-extra",
    ],
)
def test_identity_rejects_different_person(url: str) -> None:
    reason = identity_rejection_reason("alex-testfounder-1", url)
    assert reason == "profile LinkedIn URL does not match the requested founder"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/in/alex-testfounder-1",
        "https://linkedin.com.evil.example/in/alex-testfounder-1",
        "https://notlinkedin.com/in/alex-testfounder-1",
        "https://www.linkedin.com/company/alex-testfounder-1",
        "https://www.linkedin.com/pub/alex-testfounder-1",
        "https://www.linkedin.com/in/",
        "https://www.linkedin.com/in/alex-testfounder-1/details",
        "https://www.linkedin.com/alex-testfounder-1",
        "ftp://www.linkedin.com/in/alex-testfounder-1",
        "javascript://www.linkedin.com/in/alex-testfounder-1",
        "https://[::1/in/alex-testfounder-1",
    ],
)
def test_identity_rejects_non_profile_urls(url: str) -> None:
    reason = identity_rejection_reason("alex-testfounder-1", url)
    assert reason == "profile LinkedIn URL is not a linkedin.com/in/ profile"


@pytest.mark.parametrize("url", [None, "", "   ", 42, ["https://www.linkedin.com/in/alex-testfounder-1"]])
def test_identity_rejects_missing_url(url: object) -> None:
    assert identity_rejection_reason("alex-testfounder-1", url) == (
        "profile has no LinkedIn URL to verify identity"
    )


def test_slug_from_linkedin_url_edge_cases() -> None:
    assert slug_from_linkedin_url(None) is None
    assert slug_from_linkedin_url("") is None
    assert slug_from_linkedin_url("https://www.linkedin.com/in/%20/") is None
    assert slug_from_linkedin_url("https://www.linkedin.com./in/abc") == "abc"


# --------------------------------------------------------------------------- #
# Email parsing + quality
# --------------------------------------------------------------------------- #


def test_parse_emails_is_tolerant() -> None:
    raw = [
        {"email": "a@x.example", "smtp_valid": " VALID ", "type": "Professional", "grade": "a-"},
        {"email": None},
        {"email": 5, "grade": 7, "smtp_valid": ""},
        "not a mapping",
        None,
    ]
    parsed = parse_emails(raw)
    assert parsed[0] == ProviderEmail(
        email="a@x.example", smtp_valid="valid", type="professional", grade="A-"
    )
    assert parsed[1] == ProviderEmail(email=None)
    assert parsed[2] == ProviderEmail(email=None)
    assert len(parsed) == 3


@pytest.mark.parametrize("raw", [None, "a@x.example", b"bytes", 5, {"email": "a@x.example"}])
def test_parse_emails_non_list(raw: object) -> None:
    assert parse_emails(raw) == []


@pytest.mark.parametrize(
    ("grade", "smtp", "ok"),
    [
        ("A", None, True),
        ("A-", None, True),
        ("A", "inconclusive", True),
        ("B", "valid", True),
        (None, "valid", True),
        ("B", None, False),
        ("B", "inconclusive", False),
        (None, None, False),
        ("A", "invalid", False),  # smtp invalid always dropped
        ("F", "valid", False),  # grade F always dropped
        ("F", None, False),
    ],
)
def test_quality_filter(grade: str | None, smtp: str | None, ok: bool) -> None:
    assert is_acceptable_quality(ProviderEmail(email="a@x.example", grade=grade, smtp_valid=smtp)) is ok


def test_normalise_domain() -> None:
    assert normalise_domain(None) is None
    assert normalise_domain("") is None
    assert normalise_domain("  WWW.Co1.Example. ") == "co1.example"
    assert normalise_domain("@co1.example") == "co1.example"
    assert normalise_domain(" @ ") is None


@pytest.mark.parametrize(
    ("email", "domain", "match"),
    [
        ("a@co1.example", "co1.example", True),
        ("a@CO1.example", "www.co1.example", True),
        ("a@uk.co1.example", "co1.example", True),
        ("a@notco1.example", "co1.example", False),
        ("a@co1.example.evil", "co1.example", False),
        ("a@co1.example", None, False),
        ("no-at-sign", "co1.example", False),
    ],
)
def test_domain_matches(email: str, domain: str | None, match: bool) -> None:
    assert domain_matches(email, domain) is match


# --------------------------------------------------------------------------- #
# Selection / preference
# --------------------------------------------------------------------------- #


def e(email: str, *, grade: str | None = "A", smtp: str | None = "valid", kind: str | None = "professional"):
    return ProviderEmail(email=email, grade=grade, smtp_valid=smtp, type=kind)


def test_prefers_professional_on_company_domain() -> None:
    result = select_email(
        [
            e("alex.t@mail.example", kind="personal"),
            e("alex@othercorp.example"),
            e("alex@co1.example", grade="A-"),
        ],
        "co1.example",
    )
    assert result.outcome is LookupOutcome.FOUND
    assert result.email == "alex@co1.example"
    assert result.grade == "A-"
    assert result.reason == "professional email on the company domain"


def test_subdomain_counts_as_company_domain() -> None:
    result = select_email([e("alex@othercorp.example"), e("alex@uk.co1.example")], "co1.example")
    assert result.email == "alex@uk.co1.example"


def test_falls_back_to_any_professional_then_personal() -> None:
    result = select_email(
        [e("alex.t@mail.example", kind="personal"), e("alex@othercorp.example")], "co1.example"
    )
    assert result.email == "alex@othercorp.example"
    assert result.reason == "professional email"

    personal = select_email([e("alex.t@mail.example", kind="personal")], "co1.example")
    assert personal.outcome is LookupOutcome.FOUND
    assert personal.email == "alex.t@mail.example"
    assert personal.reason == "personal email"


def test_without_company_domain_professional_wins() -> None:
    result = select_email([e("alex.t@mail.example", kind="personal"), e("alex@othercorp.example")], None)
    assert result.email == "alex@othercorp.example"


def test_unknown_type_ordering() -> None:
    on_domain = select_email([e("alex@othercorp.example"), e("alex@co1.example", kind=None)], "co1.example")
    assert on_domain.email == "alex@co1.example"
    assert on_domain.reason == "email on the company domain"

    unknown = select_email([e("alex.t@mail.example", kind="personal"), e("x@y.example", kind=None)], None)
    assert unknown.email == "x@y.example"
    assert unknown.reason == "email of unknown type"


def test_within_tier_prefers_better_grade_then_order() -> None:
    result = select_email(
        [
            e("one@co1.example", grade=None),
            e("two@co1.example", grade="A-"),
            e("three@co1.example"),
        ],
        "co1.example",
    )
    assert result.email == "three@co1.example"
    smtp_only = select_email([e("one@co1.example", grade=None), e("two@co1.example", grade="B")], None)
    assert smtp_only.email == "one@co1.example"
    assert smtp_only.grade is None


def test_drops_invalid_and_f_grade_even_on_company_domain() -> None:
    result = select_email(
        [
            e("bad@co1.example", grade="A", smtp="invalid"),
            e("worse@co1.example", grade="F"),
            e("ok@othercorp.example", grade="A-", smtp=None),
        ],
        "co1.example",
    )
    assert result.email == "ok@othercorp.example"


def test_none_left_is_not_found() -> None:
    result = select_email(
        [e("a@co1.example", grade="B", smtp="inconclusive"), e("b@co1.example", smtp="invalid")],
        "co1.example",
    )
    assert result.outcome is LookupOutcome.NOT_FOUND
    assert result.reason == NO_VERIFIED_EMAIL
    assert result.email is None
    assert select_email([], None).outcome is LookupOutcome.NOT_FOUND


@pytest.mark.parametrize(
    "bad",
    [
        "alex@co1.example\r\nBcc: victim@evil.example",
        "alex@co1.example\nBcc: victim@evil.example",
        "alex@co1.example, victim@evil.example",
        "alex@co1.example;victim@evil.example",
        "Alex <alex@co1.example>",
        '"alex"@co1.example',
        "alex @co1.example",
        "not-an-email",
        "",
        None,
        "a" * 250 + "@co1.example",
    ],
)
def test_header_injection_ish_emails_rejected(bad: str | None) -> None:
    result = select_email([e(bad)], "co1.example")  # type: ignore[arg-type]
    assert result.outcome is LookupOutcome.NOT_FOUND


def test_injection_entry_skipped_but_clean_one_used() -> None:
    result = select_email(
        [e("alex@co1.example\r\nBcc: victim@evil.example"), e("alex.clean@co1.example")],
        "co1.example",
    )
    assert result.email == "alex.clean@co1.example"


def test_duplicate_addresses_collapsed() -> None:
    result = select_email([e("Alex@Co1.example", kind="personal"), e("alex@co1.example")], "co1.example")
    # First occurrence wins the dedupe; its (personal) type is used.
    assert result.reason == "personal email"


def test_email_is_normalised() -> None:
    result = select_email([e("  alex@CO1.EXAMPLE ")], "co1.example")
    assert result.email == "alex@co1.example"


# --------------------------------------------------------------------------- #
# evaluate_profile
# --------------------------------------------------------------------------- #


def test_evaluate_profile_found() -> None:
    p = {
        "linkedin_url": "https://uk.linkedin.com/in/alex-testfounder-1/",
        "emails": [{"email": "alex@co1.example", "grade": "A", "type": "professional"}],
    }
    result = evaluate_profile("alex-testfounder-1", p, "co1.example")
    assert result.outcome is LookupOutcome.FOUND
    assert result.email == "alex@co1.example"
    assert result.grade == "A"


def test_evaluate_profile_identity_mismatch_rejected_even_with_good_email() -> None:
    p = {
        "linkedin_url": "https://www.linkedin.com/in/someone-else-9",
        "emails": [{"email": "someone@co1.example", "grade": "A", "type": "professional"}],
    }
    result = evaluate_profile("alex-testfounder-1", p, "co1.example")
    assert result.outcome is LookupOutcome.REJECTED
    assert result.email is None
    assert "does not match" in (result.reason or "")


def test_evaluate_profile_missing_url_rejected() -> None:
    result = evaluate_profile("alex-testfounder-1", {"emails": []}, None)
    assert result.outcome is LookupOutcome.REJECTED


def test_evaluate_profile_no_emails_not_found() -> None:
    p = {"linkedin_url": "https://www.linkedin.com/in/alex-testfounder-1"}
    result = evaluate_profile("alex-testfounder-1", p, None)
    assert result.outcome is LookupOutcome.NOT_FOUND
    assert result.reason == NO_VERIFIED_EMAIL
