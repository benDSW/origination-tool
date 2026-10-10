"""Pure filter translation: saved-search filters <-> Evertrace tool arguments."""

from __future__ import annotations

import logging

import pytest

from app.domain.models import SearchFilter
from app.integrations.evertrace.filters import (
    parse_saved_filter,
    to_create_search_filters,
    to_epoch_ms,
    to_list_signals_arguments,
    validate_search_title,
)
from tests.fakes import utc


def f(key: str, *values: str, operator: str = "in") -> SearchFilter:
    return SearchFilter(key=key, operator=operator, values=values)


def test_array_keys_become_lists_and_keep_exclusions() -> None:
    args = to_list_signals_arguments(
        (
            f("city", "!London"),
            f("status", "New Company", "New Patent"),
            f("country", "United Kingdom"),
            f("industry", "Robotics"),
            f("profile_tags", "Serial Founder"),
            f("gender", "woman"),
        )
    )
    assert args == {
        "city": ["!London"],
        "status": ["New Company", "New Patent"],
        "country": ["United Kingdom"],
        "industry": ["Robotics"],
        "profile_tags": ["Serial Founder"],
        "gender": ["woman"],
    }


def test_scalar_and_renamed_keys() -> None:
    args = to_list_signals_arguments(
        (
            f("score", "7", operator="eq"),
            f("investor_interest_n", "3", operator="eq"),
            f("time_relative", "last_30_days", operator="eq"),
            f("time_range", "2026-01-01", "2026-02-01"),
        )
    )
    assert args == {
        "score": "7",
        "ii_n": "3",
        "time_relative": "last_30_days",
        "time_range": ["2026-01-01", "2026-02-01"],
    }


def test_multi_value_score_uses_lowest(caplog: pytest.LogCaptureFixture) -> None:
    assert to_list_signals_arguments((f("score", "8", "6", "9"),)) == {"score": "6"}
    assert to_list_signals_arguments((f("score", "high", "low"),)) == {"score": "high"}


def test_unsupported_keys_are_dropped_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        args = to_list_signals_arguments(
            (
                f("location", "Somewhere"),
                f("investor_interest_firm", "Firm A"),
                f("investor_interest_investor", "Person B"),
                f("totally_unknown", "x"),
                f("country", "United Kingdom"),
            )
        )
    assert args == {"country": ["United Kingdom"]}
    for key in ("location", "investor_interest_firm", "investor_interest_investor", "totally_unknown"):
        assert key in caplog.text


def test_created_after_takes_the_later_value() -> None:
    ours = to_epoch_ms(utc(2026, 10, 1))
    earlier = str(to_epoch_ms(utc(2026, 1, 1)))
    later = str(to_epoch_ms(utc(2026, 10, 5)))
    assert to_list_signals_arguments((f("created_after", earlier),), created_after_ms=ours) == {
        "created_after": str(ours)
    }
    assert to_list_signals_arguments((f("created_after", later),), created_after_ms=ours) == {
        "created_after": later
    }
    assert to_list_signals_arguments((f("created_after", later),)) == {"created_after": later}
    assert to_list_signals_arguments((), created_after_ms=ours) == {"created_after": str(ours)}


def test_created_after_iso_and_garbage() -> None:
    iso = to_list_signals_arguments((f("created_after", "2026-10-05T00:00:00Z"),), created_after_ms=0)
    assert iso == {"created_after": str(to_epoch_ms(utc(2026, 10, 5)))}
    naive = to_list_signals_arguments((f("created_after", "2026-10-05"),))
    assert naive == {"created_after": str(to_epoch_ms(utc(2026, 10, 5)))}
    assert to_list_signals_arguments((f("created_after", "soon"),), created_after_ms=5) == {
        "created_after": "5"
    }


def test_negative_operators_become_exclusions_and_unknown_operators_dropped() -> None:
    args = to_list_signals_arguments(
        (f("city", "London", "!Slough", operator="nin"), f("region", "North West", operator="between"))
    )
    assert args == {"city": ["!London", "!Slough"]}


def test_duplicate_keys_merge_and_empty_values_skipped() -> None:
    args = to_list_signals_arguments(
        (f("status", "New Company"), f("status", "New Company", "Stealth"), f("industry", " ", ""))
    )
    assert args == {"status": ["New Company", "Stealth"]}


def test_bad_time_range_dropped() -> None:
    assert to_list_signals_arguments((f("time_range", "2026-01-01"),)) == {}


def test_to_epoch_ms_requires_aware() -> None:
    from datetime import datetime

    with pytest.raises(ValueError):
        to_epoch_ms(datetime(2026, 1, 1))  # noqa: DTZ001
    assert to_epoch_ms(utc(1970, 1, 1, 0, 0, 1)) == 1000


def test_create_search_filters_encoding() -> None:
    encoded = to_create_search_filters(
        (f("status", "New Company", "New Patent"), f("score", "7", operator="eq"))
    )
    assert encoded == [
        {"key": "status", "operator": "in", "value": "New Company|New Patent"},
        {"key": "score", "operator": "eq", "value": "7"},
    ]


@pytest.mark.parametrize(
    "bad",
    [
        f("not_a_key", "x"),
        f("status", "a", operator="between"),
        f("status", " "),
        f("city", "A|B"),
        f("score", "6", "7", operator="eq"),
    ],
)
def test_create_search_filters_validation(bad: SearchFilter) -> None:
    with pytest.raises(ValueError):
        to_create_search_filters((bad,))


def test_validate_search_title() -> None:
    assert validate_search_title("  North founders  ") == "North founders"
    assert validate_search_title("x" * 50) == "x" * 50
    for bad in ("", "   ", "x" * 51):
        with pytest.raises(ValueError):
            validate_search_title(bad)


def test_parse_saved_filter_shapes() -> None:
    assert parse_saved_filter({"key": "city", "operator": "in", "value": "!London"}) == f("city", "!London")
    assert parse_saved_filter({"key": "status", "value": "A|B| "}) == f("status", "A", "B")
    assert parse_saved_filter({"key": "score", "operator": "eq", "value": 7}) == f(
        "score", "7", operator="eq"
    )
    assert parse_saved_filter({"key": "x", "operator": "in", "value": ["a", None, "b"]}) == f("x", "a", "b")
    assert parse_saved_filter({"key": "x", "value": None}) == f("x")
    assert parse_saved_filter({"key": " "}) is None
    assert parse_saved_filter("nope") is None
