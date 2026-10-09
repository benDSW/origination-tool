"""R6: founders already seen on the Affinity opportunity lists are recognised."""

import pytest

from app.domain.matching import (
    KnownIndex,
    find_crm_match,
    normalise_company_name,
    normalise_domain,
    normalise_person_name,
)
from app.domain.models import CompanyRef, KnownEntities, KnownEntity

KNOWN = KnownEntities(
    entities=(
        KnownEntity(
            list_name="Origination Opportunities",
            opportunity_name="Lacurve",
            company_names=("Lacurve",),
            company_domains=("lacurve.io",),
            person_names=("Sam Founder",),
        ),
        KnownEntity(
            list_name="Applicant Opportunities",
            opportunity_name="Widget Robotics - Seed",
            company_names=("Widget Robotics Ltd",),
            person_names=("Prince",),  # single token: too ambiguous to match on
        ),
    )
)
INDEX = KnownIndex.build(KNOWN)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Lacurve Limited", "lacurve"),
        ("The Acme Co. (UK) Ltd", "acme"),
        ("Widget Robotics LTD.", "widget robotics"),
        ("Smith & Jones PLC", "smith and jones"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalise_company_name(raw: str | None, expected: str) -> None:
    assert normalise_company_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.Acme.co.uk/about", "acme.co.uk"),
        ("acme.io", "acme.io"),
        ("WWW.x.com.", "x.com"),
        (None, ""),
    ],
)
def test_normalise_domain(raw: str | None, expected: str) -> None:
    assert normalise_domain(raw) == expected


def test_normalise_person_name() -> None:
    assert normalise_person_name("  Sam  O'Founder ") == "sam o founder"


def test_match_on_company_name_ignores_legal_suffix() -> None:
    m = find_crm_match(INDEX, CompanyRef(name="Lacurve Limited"), "Someone Else")
    assert m is not None and m.matched_on == "company_name"
    assert "Origination Opportunities" in m.describe()


def test_match_on_domain_takes_precedence() -> None:
    m = find_crm_match(INDEX, CompanyRef(name="Renamed Co", website_domain="https://lacurve.io"), "X Y")
    assert m is not None and m.matched_on == "company_domain"


def test_match_on_opportunity_name_when_no_company_names() -> None:
    idx = KnownIndex.build(KnownEntities(entities=(KnownEntity(list_name="L", opportunity_name="Foo Ltd"),)))
    assert find_crm_match(idx, CompanyRef(name="Foo"), "a b") is not None


def test_match_on_founder_full_name() -> None:
    m = find_crm_match(INDEX, CompanyRef(name="Brand New Ltd"), "Sam Founder")
    assert m is not None and m.matched_on == "person_name"


def test_single_token_person_names_are_not_matched() -> None:
    assert find_crm_match(INDEX, None, "Prince") is None


def test_new_founder_does_not_match() -> None:
    assert find_crm_match(INDEX, CompanyRef(name="Totally New Ltd"), "New Person") is None
    assert find_crm_match(INDEX, None, "") is None
