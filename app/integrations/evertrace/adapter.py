"""Evertrace adapter: implements the ``SearchCatalog`` and ``SignalSource`` ports.

Raw tool payloads are mapped to domain models here and never leave this module.
Evertrace data is untrusted: every field is type-checked, LinkedIn slugs and
URLs are validated, and anything that does not fit is dropped rather than passed on.

Data-loss policy for ``iter_signals``: if fetching ONE signal's detail fails, the
whole iteration raises :class:`IntegrationError`. The service then records the
error and does NOT advance that search's watermark, so the signal is retried on
the next run instead of being silently skipped. A record that is permanently
unusable (no id / no discovery time) is logged and skipped, because retrying it
would block the search forever without ever succeeding.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.domain.models import (
    CompanyRef,
    NewSearchSpec,
    SavedSearch,
    SearchFilter,
    SearchVocabulary,
    Signal,
)
from app.domain.ports import IntegrationError, NotFoundError
from app.integrations.evertrace.filters import (
    parse_saved_filter,
    to_create_search_filters,
    to_epoch_ms,
    to_list_signals_arguments,
    validate_search_title,
)
from app.integrations.evertrace.transport import SYSTEM, McpTransport, ToolError, ToolTransport

logger = logging.getLogger(__name__)

PAGE_LIMIT = 100
MAX_PAGES = 100
MAX_CITY_QUERY = 100
MAX_CITY_LIMIT = 50

_LINKEDIN_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.%-]{1,99}")
_HOSTNAME = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_FOUNDER_TITLE = re.compile(r"founder|\bceo\b|\bowner\b|managing\s+director", re.IGNORECASE)
_UNSAFE_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f<>\"'`\\]")


# --------------------------------------------------------------------------- #
# Pure mapping helpers (module-level so they are unit-testable in isolation)
# --------------------------------------------------------------------------- #


def _str(value: object) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(s for s in (_str(v) for v in value) if s)


def parse_linkedin_slug(raw: object) -> str | None:
    """Only a bare, conservative slug is accepted (it is later built into a URL)."""
    value = _str(raw)
    return value if value and _LINKEDIN_SLUG.fullmatch(value) else None


def parse_epoch_ms(raw: object) -> datetime | None:
    millis = _int(raw)
    if millis is not None:
        try:
            return datetime.fromtimestamp(millis / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = _str(raw)
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def website_domain(raw: object) -> str | None:
    """Bare lowercase host (no ``www.``) from an http(s) URL or a bare host; else None."""
    value = _str(raw)
    if not value or _UNSAFE_URL_CHARS.search(value):
        return None
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    host = host.lower().rstrip(".").removeprefix("www.")
    return host if _HOSTNAME.fullmatch(host) else None


def registry_url(raw: object) -> str | None:
    """Absolute https URL for a company-registry link (Evertrace omits the scheme)."""
    value = _str(raw)
    if not value or _UNSAFE_URL_CHARS.search(value):
        return None
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return None
    if not _HOSTNAME.fullmatch(host.lower()):
        return None
    return value


def select_current_company(experiences: object) -> CompanyRef | None:
    """The founder's current company: an open-ended role, founder-like titles first,
    then the lowest ``indexOrder`` (Evertrace's own ordering)."""
    if not isinstance(experiences, list):
        return None
    candidates: list[tuple[bool, int, int, dict[str, Any], str]] = []
    for position, exp in enumerate(experiences):
        if not isinstance(exp, dict) or _str(exp.get("endDate")) is not None:
            continue
        entity = exp.get("entity") if isinstance(exp.get("entity"), dict) else {}
        name = _str(exp.get("companyName")) or _str(entity.get("name"))
        if not name:
            continue
        title = _str(exp.get("title")) or ""
        index_order = _int(exp.get("indexOrder"))
        candidates.append(
            (
                not bool(_FOUNDER_TITLE.search(title)),
                index_order if index_order is not None else 1_000_000,
                position,
                entity,
                name,
            )
        )
    if not candidates:
        return None
    _, _, _, entity, name = min(candidates, key=lambda c: (c[0], c[1], c[2]))
    return CompanyRef(
        name=name,
        website_domain=website_domain(entity.get("websiteUrl")),
        registry_url=registry_url(entity.get("sourceUrl")),
    )


def map_signal(detail: Mapping[str, Any], compact: Mapping[str, Any] | None = None) -> Signal | None:
    """Full (``compact=false``) signal payload -> domain Signal; None if unusable.

    ``compact`` (the list item) fills gaps the detail payload leaves.
    """
    fallback: Mapping[str, Any] = compact or {}

    def pick(key: str) -> Any:
        value = detail.get(key)
        return value if value is not None else fallback.get(key)

    external_id = _str(pick("id"))
    discovered_at = parse_epoch_ms(pick("discoveredAt"))
    if not external_id or discovered_at is None:
        return None
    return Signal(
        external_id=external_id,
        first_name=_str(pick("firstName")) or "",
        last_name=_str(pick("lastName")) or "",
        email=_str(pick("email")),
        linkedin_slug=parse_linkedin_slug(pick("linkedinIdStr")),
        city=_str(pick("city")),
        country=_str(pick("country")),
        gender=_str(pick("gender")),
        score=_int(pick("score")),
        signal_type=_str(pick("signalType")),
        discovered_at=discovered_at,
        summary=_str(pick("summary")),
        industries=_str_tuple(pick("industries")),
        company=select_current_company(detail.get("experiences")),
    )


def map_saved_search(payload: object) -> SavedSearch:
    if not isinstance(payload, dict):
        raise IntegrationError(SYSTEM, "search payload was not an object")
    external_id = _str(payload.get("id"))
    if not external_id:
        raise IntegrationError(SYSTEM, "search payload had no id")
    raw_filters = payload.get("filters")
    filters = tuple(
        f
        for f in (parse_saved_filter(r) for r in (raw_filters if isinstance(raw_filters, list) else []))
        if f is not None
    )
    return SavedSearch(
        external_id=external_id, title=_str(payload.get("title")) or external_id, filters=filters
    )


def _vocab_values(raw: object) -> tuple[str, ...]:
    """A vocabulary list of strings or ``{value|name: ...}`` objects."""
    if not isinstance(raw, list):
        return ()
    values: list[str] = []
    for item in raw:
        value = _str(item.get("value") or item.get("name")) if isinstance(item, dict) else _str(item)
        if value and value not in values:
            values.append(value)
    return tuple(values)


def map_vocabulary(payload: object) -> SearchVocabulary:
    if not isinstance(payload, dict):
        raise IntegrationError(SYSTEM, "metadata payload was not an object")
    score_range = payload.get("scoreRange") if isinstance(payload.get("scoreRange"), dict) else {}
    score_min = _int(score_range.get("min"))
    score_max = _int(score_range.get("max"))
    return SearchVocabulary(
        statuses=_vocab_values(payload.get("statuses")),
        industries=_vocab_values(payload.get("industries")),
        countries=_vocab_values(payload.get("countries")),
        regions=_vocab_values(payload.get("regions")),
        genders=_vocab_values(payload.get("genders")),
        profile_tags=_vocab_values(payload.get("profileTags")),
        age_ranges=_vocab_values(payload.get("ageRanges")),
        education_levels=_vocab_values(payload.get("educationLevels")),
        customer_focus=_vocab_values(payload.get("customerFocus")),
        score_min=score_min if score_min is not None else 1,
        score_max=score_max if score_max is not None else 10,
    )


def _items(payload: object, *keys: str) -> list[Any]:
    """A list payload, or the first list found under one of ``keys``."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def map_cities(payload: object, limit: int) -> list[str]:
    cities: list[str] = []
    for item in _items(payload, "items", "data", "cities", "results"):
        if isinstance(item, dict):
            name = _str(item.get("name")) or _str(item.get("city")) or _str(item.get("value"))
        else:
            name = _str(item)
        if name and name not in cities:
            cities.append(name)
    return cities[:limit]


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #


class EvertraceAdapter:
    """One object for both Evertrace ports, sharing a single throttled transport."""

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        transport: ToolTransport | None = None,
    ) -> None:
        self._transport: ToolTransport = transport or McpTransport(
            api_key, http_client=http_client, sleep=sleep, monotonic=monotonic
        )

    def __repr__(self) -> str:
        return "EvertraceAdapter()"

    def close(self) -> None:
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()

    # ----------------------------------------------------------- SearchCatalog

    def list_searches(self) -> list[SavedSearch]:
        payload = self._transport.call_tool("list_searches", {})
        searches: list[SavedSearch] = []
        for item in _items(payload, "items", "data"):
            if not isinstance(item, dict):
                continue
            external_id = _str(item.get("id"))
            if not external_id:
                continue
            searches.append(
                SavedSearch(external_id=external_id, title=_str(item.get("title")) or external_id)
            )
        return searches

    def get_search(self, external_id: str) -> SavedSearch:
        external_id = external_id.strip()
        if not external_id:
            raise NotFoundError("empty search id")
        try:
            payload = self._transport.call_tool("get_search", {"id": external_id, "compact": False})
        except ToolError as exc:
            if "not found" in exc.tool_message.lower():
                raise NotFoundError(external_id) from None
            raise
        return map_saved_search(payload)

    def create_search(self, spec: NewSearchSpec) -> SavedSearch:
        title = validate_search_title(spec.title)
        filters = to_create_search_filters(spec.filters)
        payload = self._transport.call_tool("create_search", {"title": title, "filters": filters})
        created = map_saved_search(payload)
        if not created.filters and spec.filters:
            # Some responses omit filters; fall back to what we asked for.
            created = created.model_copy(update={"filters": spec.filters})
        return created

    def vocabulary(self) -> SearchVocabulary:
        return map_vocabulary(self._transport.call_tool("get_signals_metadata", {}))

    def search_cities(self, query: str, limit: int = 20) -> list[str]:
        query = query.strip()
        if not query or len(query) > MAX_CITY_QUERY:
            return []
        limit = max(1, min(limit, MAX_CITY_LIMIT))
        payload = self._transport.call_tool("search_cities", {"search": query, "limit": limit})
        return map_cities(payload, limit)

    # ------------------------------------------------------------ SignalSource

    def iter_signals(self, filters: tuple[SearchFilter, ...], since: datetime) -> Iterator[Signal]:
        """Yield every signal matching ``filters`` discovered at or after ``since``.

        Raises IntegrationError if any page or any signal's detail cannot be
        fetched (see the module docstring for why we never skip silently).
        """
        since_ms = to_epoch_ms(since)
        base_args = to_list_signals_arguments(filters, created_after_ms=since_ms)
        seen: set[str] = set()
        page = 1
        while True:
            payload = self._transport.call_tool(
                "list_signals", {**base_args, "page": page, "limit": PAGE_LIMIT}
            )
            items = _items(payload, "data", "items")
            for item in items:
                signal = self._fetch_signal(item, since_ms, seen)
                if signal is not None:
                    yield signal
            if len(items) < _effective_limit(payload) or not items:
                return
            if page >= MAX_PAGES:
                logger.error(
                    "Evertrace pagination hit the %d-page cap; older signals in this window were skipped",
                    MAX_PAGES,
                )
                return
            page += 1

    def _fetch_signal(self, item: object, since_ms: int, seen: set[str]) -> Signal | None:
        if not isinstance(item, dict):
            return None
        signal_id = _str(item.get("id"))
        if not signal_id:
            logger.warning("Skipping Evertrace list item without an id")
            return None
        if signal_id in seen:
            return None
        seen.add(signal_id)
        discovered = parse_epoch_ms(item.get("discoveredAt"))
        if discovered is not None and to_epoch_ms(discovered) < since_ms:
            return None
        try:
            detail = self._transport.call_tool("get_signal", {"id": signal_id, "compact": False})
        except IntegrationError as exc:
            logger.warning("Fetching Evertrace signal %s failed: %s", signal_id, exc)
            raise IntegrationError(
                SYSTEM,
                f"could not fetch signal {signal_id}: {str(exc).removeprefix(f'[{SYSTEM}] ')}",
                retryable=exc.retryable,
            ) from None
        if not isinstance(detail, dict):
            raise IntegrationError(SYSTEM, f"signal {signal_id} detail was not an object")
        signal = map_signal(detail, item)
        if signal is None:
            logger.warning("Skipping unusable Evertrace signal %s (missing id or discovery time)", signal_id)
            return None
        if to_epoch_ms(signal.discovered_at) < since_ms:
            return None
        return signal


def _effective_limit(payload: object) -> int:
    """The page size the server actually applied (it may cap ours), so a short
    page is only treated as the last page when it really is."""
    if isinstance(payload, dict) and isinstance(payload.get("meta"), dict):
        limit = _int(payload["meta"].get("limit"))
        if limit is not None and 0 < limit <= PAGE_LIMIT:
            return limit
    return PAGE_LIMIT
