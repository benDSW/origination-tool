"""R5: per-search location policy."""

import pytest

from app.domain.eligibility import evaluate_location
from app.domain.geography import is_inside_m25, is_uk, normalise_place
from app.domain.models import LocationPolicy
from tests.fakes import make_signal

DEFAULT = LocationPolicy.UK_OUTSIDE_LONDON
LONDON_OK = LocationPolicy.LONDON_ALLOWED


@pytest.mark.parametrize(
    "city", ["Manchester", "Edinburgh", "Cambridge", "Bristol", "Leeds", "Belfast", None]
)
def test_regional_uk_founders_are_eligible(city: str | None) -> None:
    assert evaluate_location(make_signal(city=city), DEFAULT).eligible


@pytest.mark.parametrize(
    "city",
    [
        "London",
        "london",
        "Greater London",
        "Croydon",
        "Watford",
        "Shoreditch",
        "Kingston upon Thames",
        "Croydon, England, United Kingdom",
        "London Borough of Hackney",
        "Camden, London",
    ],
)
def test_london_and_inside_m25_are_excluded_by_default(city: str) -> None:
    decision = evaluate_location(make_signal(city=city), DEFAULT)
    assert not decision
    assert "London" in (decision.reason or "")


@pytest.mark.parametrize("city", ["London", "Croydon", "Manchester"])
def test_london_allowed_policy_admits_london(city: str) -> None:
    assert evaluate_location(make_signal(city=city), LONDON_OK).eligible


@pytest.mark.parametrize("country", ["France", "United States", None, ""])
def test_non_uk_is_excluded_under_every_policy(country: str | None) -> None:
    for policy in LocationPolicy:
        decision = evaluate_location(make_signal(country=country), policy)
        assert not decision.eligible
        assert "Not UK" in (decision.reason or "")


@pytest.mark.parametrize(
    "country", ["United Kingdom", "UK", "England", "Scotland", "Wales", "Northern Ireland"]
)
def test_uk_country_variants(country: str) -> None:
    assert is_uk(country)


def test_normalise_place_strips_country_suffixes() -> None:
    assert normalise_place("  Croydon,  England, United Kingdom ") == "croydon"
    assert normalise_place(None) == ""


def test_is_inside_m25_handles_empty() -> None:
    assert not is_inside_m25(None)
    assert not is_inside_m25("")


def test_decision_repr_is_informative() -> None:
    assert "eligible=True" in repr(evaluate_location(make_signal(), DEFAULT))
