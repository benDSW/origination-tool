"""Thin, defensive HTTP client for the RocketReach v2 API.

Responsibilities: auth header, hard-coded base URL, timeouts, client-side
throttling of person lookups, bounded retries (429 / 5xx / timeouts) and async
status polling. It returns decoded JSON to the adapter only; nothing here
leaves the integration package.

Security: the API key travels only in the ``Api-Key`` request header, redirects
are never followed (so the key cannot be forwarded elsewhere), and exception
messages carry the HTTP status and endpoint path only - never the key, headers
or response bodies.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from app.domain.ports import IntegrationError

logger = logging.getLogger(__name__)

SYSTEM = "rocketreach"
BASE_URL = "https://api.rocketreach.co/api/v2"
LOOKUP_PATH = "/person/lookup"
CHECK_STATUS_PATH = "/person/checkStatus"
ACCOUNT_PATH = "/account/"

COMPLETE = "complete"
FAILED = "failed"

Sleep = Callable[[float], None]
Clock = Callable[[], float]


class Throttle:
    """Enforce a minimum interval between calls (RocketReach: 15 lookups/min).

    ``sleep`` and ``clock`` are injected so tests never actually wait.
    """

    def __init__(self, min_interval: float, *, sleep: Sleep, clock: Clock) -> None:
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self._min_interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


class RocketReachClient:
    """Low-level RocketReach API access. Use :class:`RocketReachAdapter` from services."""

    def __init__(
        self,
        api_key: str | SecretStr,
        *,
        http: httpx.Client | None = None,
        timeout: float = 15.0,
        sleep: Sleep = time.sleep,
        clock: Clock = time.monotonic,
        min_lookup_interval: float = 4.1,
        poll_interval: float = 2.0,
        poll_timeout: float = 30.0,
        max_retries: int = 2,
        backoff_base: float = 1.0,
        max_retry_after: float = 60.0,
    ) -> None:
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        self._api_key = SecretStr(key.strip())
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=timeout, follow_redirects=False)
        self._timeout = httpx.Timeout(timeout, connect=min(timeout, 5.0))
        self._sleep = sleep
        self._throttle = Throttle(min_lookup_interval, sleep=sleep, clock=clock)
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._max_retry_after = max_retry_after

    def __repr__(self) -> str:  # never expose the key
        return f"RocketReachClient(base_url={BASE_URL!r})"

    @property
    def configured(self) -> bool:
        return bool(self._api_key.get_secret_value())

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> RocketReachClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def lookup_person(self, linkedin_url: str) -> dict[str, Any] | None:
        """Look a profile up by LinkedIn URL, polling until it settles or the poll budget runs out.

        Returns the profile (whose ``status`` may still be pending if polling
        timed out), or None when RocketReach has no such profile (HTTP 404).
        """
        data = self._get_json(LOOKUP_PATH, {"linkedin_url": linkedin_url}, throttled=True)
        if data is None:
            return None
        profile = _as_profile(data, LOOKUP_PATH)
        return self._poll_until_settled(profile)

    def check_status(self, profile_id: int) -> list[dict[str, Any]] | None:
        """Current state of a pending lookup (None on 404)."""
        data = self._get_json(CHECK_STATUS_PATH, {"ids": str(profile_id)}, throttled=False)
        if data is None:
            return None
        if isinstance(data, Mapping):
            inner = data.get("profiles")
            data = inner if isinstance(inner, list) else [data]
        if not isinstance(data, list):
            raise IntegrationError(SYSTEM, f"unexpected response shape from {CHECK_STATUS_PATH}")
        return [dict(p) for p in data if isinstance(p, Mapping)]

    def account(self) -> dict[str, Any]:
        data = self._get_json(ACCOUNT_PATH, None, throttled=False)
        if not isinstance(data, Mapping):
            raise IntegrationError(SYSTEM, f"unexpected response shape from {ACCOUNT_PATH}")
        return dict(data)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _poll_until_settled(self, profile: dict[str, Any]) -> dict[str, Any] | None:
        waited = 0.0
        while profile.get("status") not in (COMPLETE, FAILED):
            profile_id = profile.get("id")
            if not isinstance(profile_id, int) or isinstance(profile_id, bool):
                return profile  # nothing to poll with; caller treats it as pending
            if waited >= self._poll_timeout:
                return profile
            self._sleep(self._poll_interval)
            waited += self._poll_interval
            profiles = self.check_status(profile_id)
            if profiles is None:
                return None
            match = next((p for p in profiles if p.get("id") == profile_id), None)
            if match is None and len(profiles) == 1:
                match = profiles[0]
            if match is not None:
                # checkStatus may return a slimmer record; keep earlier fields as fallbacks.
                profile = {**profile, **match}
        return profile

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        backoff = self._backoff_base * (2**attempt)
        if response is None:
            return backoff
        raw = response.headers.get("Retry-After")
        if raw is None:
            return backoff
        try:
            seconds = float(raw)
        except ValueError:
            return backoff
        if not math.isfinite(seconds) or seconds < 0:
            return backoff
        return min(seconds, self._max_retry_after)

    def _get_json(self, path: str, params: Mapping[str, str] | None, *, throttled: bool) -> Any | None:
        """GET ``BASE_URL + path``. Returns decoded JSON, or None on 404."""
        if not self.configured:
            raise IntegrationError(SYSTEM, "API key is not configured", retryable=False)
        url = BASE_URL + path
        headers = {"Api-Key": self._api_key.get_secret_value(), "Accept": "application/json"}
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            last_attempt = attempt == attempts - 1
            if throttled:
                self._throttle.wait()
            try:
                response = self._http.get(url, params=params, headers=headers, timeout=self._timeout)
            except httpx.TimeoutException:
                logger.warning("rocketreach %s timed out (attempt %d/%d)", path, attempt + 1, attempts)
                if last_attempt:
                    raise IntegrationError(SYSTEM, f"timeout calling {path}") from None
                self._sleep(self._retry_delay(attempt, None))
                continue
            except httpx.HTTPError as exc:
                logger.warning(
                    "rocketreach %s transport error %s (attempt %d/%d)",
                    path,
                    type(exc).__name__,
                    attempt + 1,
                    attempts,
                )
                if last_attempt:
                    raise IntegrationError(
                        SYSTEM, f"transport error calling {path} ({type(exc).__name__})"
                    ) from None
                self._sleep(self._retry_delay(attempt, None))
                continue

            status = response.status_code
            if status == 404:
                return None
            if status in (401, 403):
                raise IntegrationError(SYSTEM, f"HTTP {status} from {path}: check API key", retryable=False)
            if status == 429 or status >= 500:
                logger.warning("rocketreach %s HTTP %d (attempt %d/%d)", path, status, attempt + 1, attempts)
                if last_attempt:
                    raise IntegrationError(SYSTEM, f"HTTP {status} from {path} after {attempts} attempts")
                self._sleep(self._retry_delay(attempt, response))
                continue
            if not 200 <= status < 300:
                raise IntegrationError(SYSTEM, f"HTTP {status} from {path}", retryable=False)
            try:
                return response.json()
            except ValueError:
                raise IntegrationError(SYSTEM, f"invalid JSON from {path}") from None
        raise IntegrationError(SYSTEM, f"no response from {path}")  # pragma: no cover - loop always returns


def _as_profile(data: Any, path: str) -> dict[str, Any]:
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], Mapping):
        data = data[0]
    if not isinstance(data, Mapping):
        raise IntegrationError(SYSTEM, f"unexpected response shape from {path}")
    return dict(data)
