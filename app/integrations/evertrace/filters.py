"""Pure translation between domain search filters and Evertrace tool arguments.

Saved searches store filters as ``[{key, operator, value: "a|b"}]`` but the
``list_signals`` tool takes typed, differently-named arguments and rejects any
unknown key (``additionalProperties: false``). Everything here is side-effect
free (apart from logging) so it is cheap to unit test exhaustively.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from app.domain.models import SearchFilter

logger = logging.getLogger(__name__)

# Saved-search keys that map 1:1 to a list_signals array argument.
ARRAY_KEYS: frozenset[str] = frozenset(
    {
        "status",
        "country",
        "city",
        "region",
        "industry",
        "profile_tags",
        "gender",
        "age",
        "education_level",
        "customer_focus",
        "origin",
        "source",
        "past_companies",
        "past_education",
    }
)

# Keys create_search accepts (verified against the tool schema).
CREATE_SEARCH_KEYS: frozenset[str] = frozenset(
    {
        "status",
        "score",
        "source",
        "location",
        "country",
        "city",
        "profile_tags",
        "gender",
        "age",
        "past_companies",
        "past_education",
        "education_level",
        "customer_focus",
        "industry",
        "origin",
        "region",
        "time_relative",
        "time_range",
        "created_after",
        "investor_interest_n",
        "investor_interest_firm",
        "investor_interest_investor",
    }
)

SEARCH_TITLE_MAX = 50
VALUE_SEPARATOR = "|"
EXCLUDE_PREFIX = "!"

_POSITIVE_OPERATORS = frozenset({"in", "eq"})
_NEGATIVE_OPERATORS = frozenset({"nin", "not_in", "neq", "ne", "not"})


def to_epoch_ms(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return int(moment.timestamp() * 1000)


def _parse_created_after(raw: str) -> int | None:
    """A saved ``created_after`` as epoch ms (accepts epoch ms digits or an ISO date/datetime)."""
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return to_epoch_ms(parsed)


def _clean_values(values: Iterable[str]) -> list[str]:
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def _apply_operator(search_filter: SearchFilter) -> list[str] | None:
    """Values with exclusions expressed Evertrace-style (``!Value``); None if the operator is unknown."""
    values = _clean_values(search_filter.values)
    operator = search_filter.operator.strip().lower()
    if operator in _POSITIVE_OPERATORS:
        return values
    if operator in _NEGATIVE_OPERATORS:
        return [v if v.startswith(EXCLUDE_PREFIX) else EXCLUDE_PREFIX + v for v in values]
    return None


def _merge(existing: list[str], extra: list[str]) -> list[str]:
    return existing + [v for v in extra if v not in existing]


def to_list_signals_arguments(
    filters: Iterable[SearchFilter], *, created_after_ms: int | None = None
) -> dict[str, Any]:
    """Translate saved-search filters into ``list_signals`` arguments.

    Unsupported keys (``location``, ``investor_interest_firm``/``_investor``,
    anything unknown) are dropped with a warning - sending them would make the
    tool reject the whole call. A saved ``created_after`` is combined with
    ``created_after_ms`` by taking the later of the two (both narrow the result).
    """
    args: dict[str, Any] = {}
    saved_created_after: int | None = None
    for search_filter in filters:
        key = search_filter.key.strip()
        values = _apply_operator(search_filter)
        if values is None:
            logger.warning(
                "Dropping Evertrace filter %r: unsupported operator %r", key, search_filter.operator
            )
            continue
        if not values:
            continue
        if key in ARRAY_KEYS:
            args[key] = _merge(args.get(key, []), values)
        elif key == "score":
            args["score"] = _single_score(values)
        elif key == "investor_interest_n":
            args["ii_n"] = values[0]
        elif key == "time_relative":
            args["time_relative"] = values[0]
        elif key == "time_range":
            if len(values) != 2:
                logger.warning("Dropping Evertrace time_range filter: expected 2 values, got %d", len(values))
                continue
            args["time_range"] = values
        elif key == "created_after":
            parsed = _parse_created_after(values[0])
            if parsed is None:
                logger.warning("Dropping unparseable Evertrace created_after filter")
                continue
            saved_created_after = max(parsed, saved_created_after or parsed)
        else:
            logger.warning("Dropping Evertrace filter %r: not supported by list_signals", key)
    candidates = [v for v in (saved_created_after, created_after_ms) if v is not None]
    if candidates:
        args["created_after"] = str(max(candidates))
    return args


def _single_score(values: list[str]) -> str:
    """``list_signals`` takes one score; with several, the lowest keeps the search inclusive."""
    if len(values) == 1:
        return values[0]
    logger.warning("Evertrace score filter has %d values; using the lowest", len(values))
    numeric = [v for v in values if v.isdigit()]
    return min(numeric, key=int) if numeric else values[0]


def to_create_search_filters(filters: Iterable[SearchFilter]) -> list[dict[str, str]]:
    """Validate domain filters and encode them for ``create_search``. Raises ValueError."""
    encoded: list[dict[str, str]] = []
    for search_filter in filters:
        key = search_filter.key.strip()
        if key not in CREATE_SEARCH_KEYS:
            raise ValueError(f"Unsupported search filter key: {key!r}")
        operator = search_filter.operator.strip().lower()
        if operator not in _POSITIVE_OPERATORS:
            raise ValueError(f"Unsupported operator {search_filter.operator!r} for filter {key!r}")
        values = _clean_values(search_filter.values)
        if not values:
            raise ValueError(f"Filter {key!r} has no values")
        if any(VALUE_SEPARATOR in v for v in values):
            raise ValueError(f"Filter {key!r} values must not contain '{VALUE_SEPARATOR}'")
        if operator == "eq" and len(values) != 1:
            raise ValueError(f"Filter {key!r} with operator 'eq' takes exactly one value")
        encoded.append({"key": key, "operator": operator, "value": VALUE_SEPARATOR.join(values)})
    return encoded


def validate_search_title(title: str) -> str:
    cleaned = title.strip()
    if not 1 <= len(cleaned) <= SEARCH_TITLE_MAX:
        raise ValueError(f"Search title must be 1-{SEARCH_TITLE_MAX} characters")
    return cleaned


def parse_saved_filter(raw: object) -> SearchFilter | None:
    """One ``{key, operator, value}`` from get_search -> SearchFilter (None if malformed)."""
    if not isinstance(raw, dict):
        return None
    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        return None
    operator = raw.get("operator")
    value = raw.get("value")
    if isinstance(value, list):
        values = [str(v) for v in value if v is not None]
    elif value is None:
        values = []
    else:
        values = str(value).split(VALUE_SEPARATOR)
    return SearchFilter(
        key=key.strip(),
        operator=operator.strip() if isinstance(operator, str) and operator.strip() else "in",
        values=tuple(_clean_values(values)),
    )
