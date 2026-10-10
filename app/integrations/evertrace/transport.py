"""Evertrace transport: MCP JSON-RPC 2.0 over HTTPS.

Kept deliberately generic - ``call_tool(name, arguments) -> parsed JSON`` - so
the adapter never sees JSON-RPC, SSE or session handling, and this module can
later be swapped for Evertrace's REST API (anything satisfying
:class:`ToolTransport`) without touching ``adapter.py``.

Security notes:
- The base URL is hard-coded (no SSRF via configuration).
- Every request carries an explicit timeout.
- Exception messages never contain the API key, request headers or raw response
  bodies; tool error text is truncated to 200 characters.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from app.domain.ports import IntegrationError

logger = logging.getLogger(__name__)

SYSTEM = "evertrace"
MCP_URL = "https://api.evertrace.ai/mcp"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "dsw-origination", "version": "0.1"}

DEFAULT_TIMEOUT_S = 30.0
# Evertrace allows 60 requests/minute; a little over 1s apart keeps us under it.
DEFAULT_MIN_INTERVAL_S = 1.05
DEFAULT_MAX_RETRIES = 3
MAX_RETRY_AFTER_S = 60.0
MAX_ERROR_TEXT = 200


class ToolTransport(Protocol):
    """Anything that can invoke a named Evertrace tool and return its parsed JSON payload."""

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any: ...


class ToolError(IntegrationError):
    """The tool ran but reported a failure (``result.isError``).

    ``tool_message`` is the server's (truncated) explanation, kept separately so
    the adapter can recognise e.g. "not found" without parsing the full message.
    """

    def __init__(self, tool: str, tool_message: str) -> None:
        super().__init__(SYSTEM, f"tool {tool} failed: {tool_message}", retryable=False)
        self.tool = tool
        self.tool_message = tool_message


class _SessionExpired(Exception):
    """Internal signal: the server no longer recognises our MCP session."""


def _truncate(text: str, limit: int = MAX_ERROR_TEXT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class McpTransport:
    """Thread-safe, throttled, retrying MCP client for Evertrace.

    ``sleep`` and ``monotonic`` are injectable so tests exercise throttling and
    backoff without real waiting.
    """

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._api_key = api_key
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout_s, follow_redirects=False)
        self._sleep = sleep
        self._monotonic = monotonic
        self._timeout = httpx.Timeout(timeout_s)
        self._min_interval = min_interval_s
        self._max_retries = max_retries
        self._ids = itertools.count(1)
        self._session_id: str | None = None
        self._initialized = False
        self._last_request_at: float | None = None
        # One lock for the whole call: the rate limit and the session are shared state.
        self._lock = threading.RLock()

    def __repr__(self) -> str:  # never expose the key
        return f"McpTransport(url={MCP_URL!r}, initialized={self._initialized})"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------ public

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """Invoke ``name`` with ``arguments`` and return the tool's parsed JSON payload."""
        if not self._api_key:
            raise IntegrationError(SYSTEM, "API key is not configured", retryable=False)
        with self._lock:
            if not self._initialized:
                self._initialize()
            try:
                message = self._rpc("tools/call", {"name": name, "arguments": dict(arguments)})
            except _SessionExpired:
                logger.info("Evertrace MCP session expired; re-initialising once")
                self._reset_session()
                self._initialize()
                try:
                    message = self._rpc("tools/call", {"name": name, "arguments": dict(arguments)})
                except _SessionExpired:
                    raise IntegrationError(SYSTEM, "MCP session rejected after re-initialising") from None
        return self._tool_payload(name, message)

    # ------------------------------------------------------------- handshake

    def _reset_session(self) -> None:
        self._session_id = None
        self._initialized = False

    def _initialize(self) -> None:
        params = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO}
        try:
            self._rpc("initialize", params, capture_session=True)
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except _SessionExpired:
            raise IntegrationError(SYSTEM, "MCP initialisation was rejected") from None
        self._initialized = True

    # ---------------------------------------------------------------- JSON-RPC

    def _rpc(self, method: str, params: dict[str, Any], *, capture_session: bool = False) -> dict[str, Any]:
        request_id = next(self._ids)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        response = self._post(payload)
        if capture_session:
            session_id = response.headers.get("mcp-session-id")
            if session_id:
                self._session_id = session_id.strip()
        message = self._parse_message(response, request_id)
        error = message.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            text = error.get("message") if isinstance(error, dict) else None
            detail = _truncate(str(text)) if text else "unknown error"
            raise IntegrationError(SYSTEM, f"JSON-RPC error {code} on {method}: {detail}", retryable=False)
        return message

    def _parse_message(self, response: httpx.Response, request_id: int) -> dict[str, Any]:
        """Return the JSON-RPC message answering ``request_id`` from a JSON or SSE body."""
        content_type = response.headers.get("content-type", "").lower()
        text = response.text
        stripped = text.lstrip()
        if "text/event-stream" in content_type or stripped.startswith(("data:", "event:", "id:", ":")):
            messages = _parse_sse(text)
        else:
            try:
                decoded = json.loads(text)
            except ValueError:
                raise IntegrationError(SYSTEM, "response was not valid JSON") from None
            messages = decoded if isinstance(decoded, list) else [decoded]
        candidates = [m for m in messages if isinstance(m, dict)]
        for message in candidates:
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message
        for message in candidates:  # tolerate servers that omit / renumber the id
            if "result" in message or "error" in message:
                return message
        raise IntegrationError(SYSTEM, "response contained no JSON-RPC result")

    @staticmethod
    def _tool_payload(name: str, message: dict[str, Any]) -> Any:
        result = message.get("result")
        if not isinstance(result, dict):
            raise IntegrationError(SYSTEM, f"tool {name} returned no result")
        text = _first_text(result.get("content"))
        if result.get("isError"):
            raise ToolError(name, _truncate(text or "no details"))
        if text is not None:
            try:
                return json.loads(text)
            except ValueError:
                pass
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        raise IntegrationError(SYSTEM, f"tool {name} returned an unparseable payload")

    # -------------------------------------------------------------------- HTTP

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _throttle(self) -> None:
        now = self._monotonic()
        if self._last_request_at is not None:
            wait = self._min_interval - (now - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
                now = self._monotonic()
        self._last_request_at = now

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """POST with throttling and bounded retries. Raises IntegrationError / _SessionExpired."""
        method = str(payload.get("method"))
        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._client.post(
                    MCP_URL, json=payload, headers=self._headers(), timeout=self._timeout
                )
            except httpx.TimeoutException:
                reason, delay = "timed out", None
            except httpx.TransportError as exc:
                reason, delay = f"network error ({type(exc).__name__})", None
            else:
                status = response.status_code
                if 200 <= status < 300:
                    return response
                if status in (401, 403):
                    raise IntegrationError(SYSTEM, f"authentication failed (HTTP {status})", retryable=False)
                if status in (400, 404) and "session" in response.text.lower():
                    raise _SessionExpired()
                if status != 429 and status < 500:
                    raise IntegrationError(SYSTEM, f"{method} failed (HTTP {status})", retryable=False)
                reason, delay = f"HTTP {status}", _retry_after(response)
            if attempt >= self._max_retries:
                raise IntegrationError(SYSTEM, f"{method} failed after {attempt + 1} attempts: {reason}")
            backoff = delay if delay is not None else min(2.0**attempt, 30.0)
            logger.warning(
                "Evertrace %s %s; retrying in %.1fs (attempt %d/%d)",
                method,
                reason,
                backoff,
                attempt + 1,
                self._max_retries,
            )
            self._sleep(backoff)
            attempt += 1


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP date), capped."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    value = value.strip()
    seconds: float | None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return max(0.0, min(seconds, MAX_RETRY_AFTER_S))


def _parse_sse(text: str) -> list[Any]:
    """Decode every SSE event's ``data:`` payload as JSON (non-JSON events are ignored)."""
    messages: list[Any] = []
    data_lines: list[str] = []

    def flush() -> None:
        if data_lines:
            with contextlib.suppress(ValueError):
                messages.append(json.loads("\n".join(data_lines)))
            data_lines.clear()

    for line in text.splitlines():
        if not line.strip():
            flush()
        elif line.startswith("data:"):
            data_lines.append(line[5:].removeprefix(" "))
    flush()
    return messages


def _first_text(content: object) -> str | None:
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return item["text"]
    return None
