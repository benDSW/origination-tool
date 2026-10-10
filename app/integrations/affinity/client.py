"""Thin, read-only HTTP client for the Affinity v1 and v2 REST APIs.

Why a separate client: the adapter deals only in Affinity JSON shapes and domain
DTOs, while everything about *talking* to Affinity (auth schemes, timeouts,
retries, SSRF-safe pagination) lives here and is tested in isolation.

Safety properties:
- GET only. There is deliberately no method that can send POST/PUT/PATCH/DELETE,
  because the tool must never write to the CRM.
- Base URLs are hard-coded; pagination ``nextUrl`` values returned by the API are
  followed only if they point back at ``https://api.affinity.co/``.
- Every request has a timeout. 429/5xx/network errors are retried with
  exponential backoff (``sleep`` is injectable so tests run instantly).
- Error messages name the endpoint and status code only - never the URL (which
  can contain an email address), headers, the API key or the response body.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx
from pydantic import SecretStr

from app.domain.ports import IntegrationError

logger = logging.getLogger(__name__)

SYSTEM = "affinity"
API_ORIGIN = "https://api.affinity.co/"
V1_BASE = "https://api.affinity.co"
V2_BASE = "https://api.affinity.co/v2"
_ALLOWED_HOST = "api.affinity.co"

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_PAGES = 200
MAX_RETRY_AFTER_SECONDS = 30.0

QueryParams = Sequence[tuple[str, str | int]]
JsonObject = dict[str, Any]


def is_trusted_url(url: str) -> bool:
    """True only for absolute https URLs on the Affinity API host (SSRF guard)."""
    if not isinstance(url, str) or not url.startswith(API_ORIGIN):
        return False
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.host == _ALLOWED_HOST
        and parsed.port in (None, 443)
        and not parsed.userinfo
    )


class AffinityClient:
    """Authenticated GET-only access to Affinity. One instance per process is fine."""

    def __init__(
        self,
        api_key: str | SecretStr,
        *,
        http: httpx.Client | None = None,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not key:
            raise IntegrationError(SYSTEM, "API key is not configured", retryable=False)
        self._key = SecretStr(key)
        # Redirects are never followed: a 3xx could point anywhere.
        self._http = http or httpx.Client(timeout=timeout, follow_redirects=False)
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._backoff = backoff_seconds
        self._sleep = sleep

    def __repr__(self) -> str:  # never leak the key via repr
        return "AffinityClient(api_key=***)"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> AffinityClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def get_v2(self, path: str, params: QueryParams = (), *, label: str) -> JsonObject:
        """GET ``https://api.affinity.co/v2{path}`` with Bearer auth."""
        return self._get(V2_BASE + path, params, version=2, label=label)

    def get_v1(self, path: str, params: QueryParams = (), *, label: str) -> JsonObject:
        """GET ``https://api.affinity.co{path}`` with HTTP Basic auth (empty username)."""
        return self._get(V1_BASE + path, params, version=1, label=label)

    def iter_v2_pages(self, path: str, params: QueryParams = (), *, label: str) -> Iterator[JsonObject]:
        """Yield each v2 page, following ``pagination.nextUrl`` (cursor) safely.

        A foreign ``nextUrl`` or more than ``MAX_PAGES`` pages raises rather than
        returning a silently partial result: a partial dedupe snapshot could make
        the tool email a founder DSW already knows.
        """
        page = self.get_v2(path, params, label=label)
        pages = 1
        while True:
            yield page
            pagination = page.get("pagination")
            next_url = pagination.get("nextUrl") if isinstance(pagination, dict) else None
            if not next_url:
                return
            if not is_trusted_url(next_url):
                raise IntegrationError(SYSTEM, f"{label}: refused untrusted pagination URL", retryable=False)
            if pages >= MAX_PAGES:
                raise IntegrationError(SYSTEM, f"{label}: exceeded {MAX_PAGES} pages", retryable=False)
            page = self._get(next_url, (), version=2, label=label)
            pages += 1

    def iter_v1_pages(
        self, path: str, params: QueryParams = (), *, label: str, items_key: str
    ) -> Iterator[JsonObject]:
        """Yield each item across v1 pages, following ``next_page_token``."""
        token: str | None = None
        for _ in range(MAX_PAGES):
            page_params = list(params)
            if token:
                page_params.append(("page_token", token))
            page = self.get_v1(path, page_params, label=label)
            items = page.get(items_key)
            if isinstance(items, list):
                yield from (item for item in items if isinstance(item, dict))
            next_token = page.get("next_page_token")
            if not next_token or not isinstance(next_token, str):
                return
            token = next_token
        raise IntegrationError(SYSTEM, f"{label}: exceeded {MAX_PAGES} pages", retryable=False)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _auth_kwargs(self, version: int) -> dict[str, Any]:
        key = self._key.get_secret_value()
        if version == 2:
            return {"headers": {"Authorization": f"Bearer {key}", "Accept": "application/json"}}
        return {"auth": httpx.BasicAuth("", key), "headers": {"Accept": "application/json"}}

    def _get(self, url: str, params: QueryParams, *, version: int, label: str) -> JsonObject:
        if not is_trusted_url(url):  # defence in depth: base URLs are constants
            raise IntegrationError(SYSTEM, f"{label}: refused untrusted URL", retryable=False)
        attempt = 0
        while True:
            retry_after: float | None = None
            try:
                response = self._http.get(
                    url,
                    params=list(params) or None,
                    timeout=self._timeout,
                    **self._auth_kwargs(version),
                )
            except httpx.TimeoutException:
                failure = f"{label}: request timed out"
            except httpx.HTTPError:
                failure = f"{label}: network error"
            else:
                status = response.status_code
                if status == 200:
                    return self._json(response, label)
                if status in (401, 403):
                    raise IntegrationError(
                        SYSTEM, f"{label}: authentication failed (HTTP {status})", retryable=False
                    )
                if status == 429 or status >= 500:
                    failure = f"{label}: HTTP {status}"
                    retry_after = _retry_after_seconds(response)
                else:
                    raise IntegrationError(SYSTEM, f"{label}: unexpected HTTP {status}", retryable=False)

            if attempt >= self._max_retries:
                raise IntegrationError(SYSTEM, f"{failure} after {attempt + 1} attempts", retryable=True)
            delay = retry_after if retry_after is not None else self._backoff * (2**attempt)
            logger.warning("Affinity %s; retrying in %.1fs", failure, delay)
            self._sleep(delay)
            attempt += 1

    @staticmethod
    def _json(response: httpx.Response, label: str) -> JsonObject:
        try:
            data = response.json()
        except ValueError:
            raise IntegrationError(SYSTEM, f"{label}: response was not valid JSON") from None
        if not isinstance(data, dict):
            raise IntegrationError(SYSTEM, f"{label}: unexpected response shape")
        return data


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))
