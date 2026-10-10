"""Saved-search management (R18, R19): sync from Evertrace, local settings, new searches.

Evertrace owns the search *definitions* (title + filters); the tool owns the
*settings* (enabled, location policy, lookback, watermark). A new search is
created on Evertrace first, then registered locally.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from datetime import datetime

from pydantic import ValidationError

from app.domain.models import (
    LocationPolicy,
    NewSearchSpec,
    SearchConfig,
    SearchFilter,
    SearchVocabulary,
)
from app.domain.ports import IntegrationError, NotFoundError, SearchCatalog, Store
from app.services.clock import Clock, utc_now
from app.services.errors import ExternalServiceError, NotFound, ValidationFailed

logger = logging.getLogger(__name__)

SEARCH_TITLE_MAX = 50  # Evertrace's create_search limit (stricter than NewSearchSpec's 120)
MAX_VALUES_PER_FILTER = 50
MAX_VALUE_LEN = 100
MAX_CITY_QUERY_LEN = 100
EXCLUDE_PREFIX = "!"
LOOKBACK_MIN, LOOKBACK_MAX = 1, 730
SCORE_MIN, SCORE_MAX = 1, 10

# Filter key -> the SearchVocabulary attribute holding its closed list of values.
CLOSED_VOCABULARY_KEYS: dict[str, str] = {
    "status": "statuses",
    "industry": "industries",
    "country": "countries",
    "region": "regions",
    "gender": "genders",
    "profile_tags": "profile_tags",
    "age": "age_ranges",
    "education_level": "education_levels",
    "customer_focus": "customer_focus",
}
ALLOWED_FILTER_KEYS: frozenset[str] = frozenset({*CLOSED_VOCABULARY_KEYS, "score", "city"})
_ALLOWED_OPERATORS = frozenset({"in", "eq"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def sync_catalog(store: Store, catalog: SearchCatalog, now: datetime) -> list[SearchConfig]:
    """Register every Evertrace saved search locally (new ones start disabled).

    Raises IntegrationError if Evertrace is unavailable. Local settings are never
    overwritten (the store guarantees it).
    """
    return [store.upsert_search_from_catalog(saved, synced_at=now) for saved in catalog.list_searches()]


def city_filter(include: Iterable[str] = (), exclude: Iterable[str] = ()) -> SearchFilter | None:
    """Build Evertrace's city filter from include / exclude lists.

    Evertrace expresses an excluded city as a ``!``-prefixed value in the same
    filter (e.g. ``city in [Leeds, !London]``). Any ``!`` the user typed is
    stripped first so the include / exclude intent is always explicit.
    """
    inc = [c.strip().lstrip(EXCLUDE_PREFIX).strip() for c in include]
    exc = [c.strip().lstrip(EXCLUDE_PREFIX).strip() for c in exclude]
    values = [c for c in inc if c] + [EXCLUDE_PREFIX + c for c in exc if c]
    return SearchFilter(key="city", operator="in", values=tuple(values)) if values else None


class SearchService:
    def __init__(self, store: Store, catalog: SearchCatalog, clock: Clock = utc_now) -> None:
        self._store = store
        self._catalog = catalog
        self._clock = clock

    # ------------------------------------------------------------------ reads

    def list_configs(self) -> list[SearchConfig]:
        return self._store.list_search_configs()

    def get_config(self, search_id: int) -> SearchConfig:
        try:
            return self._store.get_search_config(search_id)
        except NotFoundError:
            raise NotFound("That search no longer exists") from None

    def find_by_external_id(self, external_id: str) -> SearchConfig | None:
        return next((c for c in self.list_configs() if c.external_id == external_id), None)

    def vocabulary(self) -> SearchVocabulary:
        try:
            return self._catalog.vocabulary()
        except IntegrationError as exc:
            raise ExternalServiceError(f"Could not load Evertrace filter options: {exc}") from None

    def search_cities(self, query: str, limit: int = 20) -> list[str]:
        query = _CONTROL.sub("", query).strip()
        if not query or len(query) > MAX_CITY_QUERY_LEN:
            return []
        try:
            return self._catalog.search_cities(query, limit=max(1, min(limit, 50)))
        except IntegrationError as exc:
            raise ExternalServiceError(f"Could not search cities on Evertrace: {exc}") from None

    # ----------------------------------------------------------------- writes

    def sync_catalog(self) -> list[SearchConfig]:
        try:
            sync_catalog(self._store, self._catalog, self._clock())
        except IntegrationError as exc:
            raise ExternalServiceError(f"Could not sync searches from Evertrace: {exc}") from None
        return self.list_configs()

    def update_config(
        self,
        search_id: int,
        *,
        enabled: bool | None = None,
        location_policy: LocationPolicy | str | None = None,
        lookback_days: int | None = None,
    ) -> SearchConfig:
        """Change local settings only; the Evertrace definition is untouched."""
        config = self.get_config(search_id)
        problems: list[str] = []
        if enabled is not None:
            config.enabled = bool(enabled)
        if location_policy is not None:
            policy = _parse_policy(location_policy, problems)
            if policy is not None:
                config.location_policy = policy
        if lookback_days is not None and _valid_lookback(lookback_days, problems):
            config.lookback_days = lookback_days
        if problems:
            raise ValidationFailed(problems)
        return self._store.save_search_config(config)

    def create_search(
        self,
        title: str,
        filters: Sequence[SearchFilter],
        *,
        location_policy: LocationPolicy | str = LocationPolicy.UK_OUTSIDE_LONDON,
        lookback_days: int = 30,
        enabled: bool = True,
    ) -> SearchConfig:
        """Create the search on Evertrace, then register it locally with these settings.

        Raises ValidationFailed for bad input (nothing is created) and
        ExternalServiceError if Evertrace fails.
        """
        problems: list[str] = []
        clean_title = _CONTROL.sub("", title or "").strip()
        if not 1 <= len(clean_title) <= SEARCH_TITLE_MAX:
            problems.append(f"Title must be 1-{SEARCH_TITLE_MAX} characters")
        policy = _parse_policy(location_policy, problems)
        _valid_lookback(lookback_days, problems)
        if not filters:
            problems.append("Add at least one filter")
        if problems:
            raise ValidationFailed(problems)

        clean_filters = validate_filters(filters, self.vocabulary())

        try:
            saved = self._catalog.create_search(NewSearchSpec(title=clean_title, filters=clean_filters))
        except IntegrationError as exc:
            raise ExternalServiceError(f"Evertrace did not create the search: {exc}") from None
        except (ValueError, ValidationError) as exc:
            raise ValidationFailed([_first_line(exc)]) from None

        config = self._store.upsert_search_from_catalog(saved, synced_at=self._clock())
        config.enabled = enabled
        config.location_policy = policy or LocationPolicy.UK_OUTSIDE_LONDON
        config.lookback_days = lookback_days
        logger.info("Created Evertrace search %s", saved.external_id)
        return self._store.save_search_config(config)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate_filters(filters: Iterable[SearchFilter], vocab: SearchVocabulary) -> tuple[SearchFilter, ...]:
    """Check user-built filters against the whitelist and Evertrace's vocabularies.

    Closed-vocabulary values are matched case-insensitively and rewritten to the
    vocabulary's spelling; a ``!`` prefix (exclusion) is allowed on closed
    vocabularies and on city. Raises ValidationFailed listing every problem.
    """
    problems: list[str] = []
    cleaned: list[SearchFilter] = []
    seen: set[str] = set()
    for search_filter in filters:
        key = search_filter.key.strip()
        if key not in ALLOWED_FILTER_KEYS:
            problems.append(f"Unknown filter '{_safe(key)}'")
            continue
        if key in seen:
            problems.append(f"Filter '{key}' is given more than once")
            continue
        seen.add(key)
        operator = search_filter.operator.strip().lower()
        if operator not in _ALLOWED_OPERATORS:
            problems.append(f"Filter '{key}' has an unsupported operator")
            continue
        values = _basic_values(key, search_filter.values, problems)
        if values is None:
            continue
        if key == "score":
            values = _score_values(values, vocab, problems)
        elif key == "city":
            values = _city_values(values, problems)
        else:
            values = _vocab_values(key, values, vocab, problems)
        if values is None:
            continue
        if operator == "eq" and len(values) != 1:
            problems.append(f"Filter '{key}' with 'eq' takes exactly one value")
            continue
        cleaned.append(SearchFilter(key=key, operator=operator, values=tuple(values)))
    if problems:
        raise ValidationFailed(problems)
    return tuple(cleaned)


def _basic_values(key: str, raw: Iterable[str], problems: list[str]) -> list[str] | None:
    values: list[str] = []
    for value in raw:
        text = value.strip() if isinstance(value, str) else ""
        if not text:
            continue
        if "|" in text or _CONTROL.search(text) or len(text) > MAX_VALUE_LEN:
            problems.append(f"Filter '{key}' has an invalid value")
            return None
        if text not in values:
            values.append(text)
    if not values:
        problems.append(f"Filter '{key}' has no values")
        return None
    if len(values) > MAX_VALUES_PER_FILTER:
        problems.append(f"Filter '{key}' has too many values (max {MAX_VALUES_PER_FILTER})")
        return None
    return values


def _score_values(values: list[str], vocab: SearchVocabulary, problems: list[str]) -> list[str] | None:
    low, high = max(SCORE_MIN, vocab.score_min), min(SCORE_MAX, vocab.score_max)
    out: list[str] = []
    for value in values:
        if not value.isdigit() or not low <= int(value) <= high:
            problems.append(f"Score must be a whole number from {low} to {high}")
            return None
        out.append(str(int(value)))
    return out


def _city_values(values: list[str], problems: list[str]) -> list[str] | None:
    include: list[str] = []
    exclude: list[str] = []
    for value in values:
        excluded = value.startswith(EXCLUDE_PREFIX)
        name = value.lstrip(EXCLUDE_PREFIX).strip()
        if not name:
            problems.append("City filter has an empty city name")
            return None
        target = exclude if excluded else include
        if name.casefold() not in {c.casefold() for c in target}:
            target.append(name)
    both = {c.casefold() for c in include} & {c.casefold() for c in exclude}
    if both:
        problems.append("A city cannot be both included and excluded")
        return None
    return include + [EXCLUDE_PREFIX + c for c in exclude]


def _vocab_values(
    key: str, values: list[str], vocab: SearchVocabulary, problems: list[str]
) -> list[str] | None:
    allowed: tuple[str, ...] = getattr(vocab, CLOSED_VOCABULARY_KEYS[key])
    if not allowed:
        problems.append(f"Evertrace offers no options for '{key}' right now")
        return None
    canonical = {a.casefold(): a for a in allowed}
    out: list[str] = []
    for value in values:
        excluded = value.startswith(EXCLUDE_PREFIX)
        name = value.lstrip(EXCLUDE_PREFIX).strip()
        match = canonical.get(name.casefold())
        if match is None:
            problems.append(f"'{_safe(name)}' is not a valid {key.replace('_', ' ')}")
            return None
        chosen = EXCLUDE_PREFIX + match if excluded else match
        if chosen not in out:  # "new company" and "New Company" are the same value
            out.append(chosen)
    return out


def _parse_policy(value: LocationPolicy | str, problems: list[str]) -> LocationPolicy | None:
    try:
        return LocationPolicy(value)
    except ValueError:
        allowed = ", ".join(p.value for p in LocationPolicy)
        problems.append(f"Location policy must be one of: {allowed}")
        return None


def _valid_lookback(days: int, problems: list[str]) -> bool:
    if isinstance(days, bool) or not isinstance(days, int) or not LOOKBACK_MIN <= days <= LOOKBACK_MAX:
        problems.append(f"Lookback must be {LOOKBACK_MIN}-{LOOKBACK_MAX} days")
        return False
    return True


def _safe(text: str, limit: int = 40) -> str:
    """User text echoed in an error message: no control characters, bounded length."""
    return _CONTROL.sub("", text)[:limit]


def _first_line(exc: Exception) -> str:
    """A short user-facing reason from an adapter ValueError (never a raw payload)."""
    if isinstance(exc, ValidationError):
        return "The search definition was rejected"
    return _safe(str(exc).splitlines()[0] if str(exc) else "The search definition was rejected", 200)


__all__ = [
    "ALLOWED_FILTER_KEYS",
    "CLOSED_VOCABULARY_KEYS",
    "SearchService",
    "city_filter",
    "sync_catalog",
    "validate_filters",
]
