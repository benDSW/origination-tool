"""A tiny in-process fake of Evertrace's MCP endpoint, mounted with respx.

Routes JSON-RPC by method, records every request, and lets tests queue raw HTTP
responses (429, 5xx, session errors...) ahead of normal tool handling.
All data used with it is synthetic.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.integrations.evertrace.transport import MCP_URL

FAKE_KEY = "sk_test_SYNTHETIC_DO_NOT_LEAK_1234567890"

ToolHandler = Callable[[dict[str, Any]], Any]


def tool_result(payload: Any, *, is_error: bool = False, text: str | None = None) -> dict[str, Any]:
    body_text = text if text is not None else json.dumps(payload)
    result: dict[str, Any] = {"content": [{"type": "text", "text": body_text}]}
    if is_error:
        result["isError"] = True
    return result


def sse(*messages: dict[str, Any]) -> str:
    return "".join(f"event: message\ndata: {json.dumps(m)}\n\n" for m in messages)


@dataclass
class FakeMcpServer:
    tools: dict[str, ToolHandler] = field(default_factory=dict)
    session_id: str | None = "sess-abc"
    use_sse: bool = False
    # Raw responses served (in order) to the next tools/call requests before normal handling.
    queued: list[httpx.Response | Exception] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]

    def methods(self) -> list[str]:
        return [b["method"] for b in self.bodies]

    def tool_calls(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (b["params"]["name"], b["params"]["arguments"])
            for b in self.bodies
            if b["method"] == "tools/call"
        ]

    def _reply(self, message: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
        headers = dict(headers or {})
        if self.use_sse:
            headers["content-type"] = "text/event-stream"
            return httpx.Response(200, text=sse(message), headers=headers)
        return httpx.Response(200, json=message, headers=headers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        method = body["method"]
        if method == "initialize":
            headers = {"Mcp-Session-Id": self.session_id} if self.session_id else {}
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {}}
            return self._reply({"jsonrpc": "2.0", "id": body["id"], "result": result}, headers)
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            if self.queued:
                item = self.queued.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            name = body["params"]["name"]
            handler = self.tools.get(name)
            if handler is None:
                result = tool_result(None, is_error=True, text=f"Unknown tool {name}")
            else:
                outcome = handler(body["params"]["arguments"])
                if isinstance(outcome, httpx.Response):
                    return outcome
                result = tool_result(outcome)
            return self._reply({"jsonrpc": "2.0", "id": body["id"], "result": result})
        return httpx.Response(400, json={"error": "unexpected method"})


class FakeClock:
    """Injected monotonic clock + sleep: sleeping advances time instantly."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


__all__ = ["FAKE_KEY", "MCP_URL", "FakeClock", "FakeMcpServer", "sse", "tool_result"]
