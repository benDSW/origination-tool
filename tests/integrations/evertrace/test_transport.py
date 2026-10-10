"""MCP transport: handshake, sessions, SSE, errors, retries and throttling (respx-mocked)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
import respx

from app.domain.ports import IntegrationError
from app.integrations.evertrace import transport as transport_module
from app.integrations.evertrace.transport import McpTransport, ToolError, _parse_sse, _retry_after
from tests.integrations.evertrace.mcp_fake import (
    FAKE_KEY,
    MCP_URL,
    FakeClock,
    FakeMcpServer,
    sse,
    tool_result,
)


@pytest.fixture
def server() -> FakeMcpServer:
    return FakeMcpServer(tools={"echo": lambda args: {"got": args}})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def mocked(server: FakeMcpServer) -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.post(MCP_URL).mock(side_effect=server)
        yield router


@pytest.fixture
def transport(mocked: respx.MockRouter, clock: FakeClock, http_client: httpx.Client) -> McpTransport:
    return McpTransport(FAKE_KEY, http_client=http_client, sleep=clock.sleep, monotonic=clock.monotonic)


def assert_no_secret(exc: BaseException) -> None:
    assert FAKE_KEY not in str(exc)
    assert FAKE_KEY not in repr(exc)


def test_handshake_then_call_with_session_and_headers(transport: McpTransport, server: FakeMcpServer) -> None:
    assert transport.call_tool("echo", {"a": 1}) == {"got": {"a": 1}}

    assert server.methods() == ["initialize", "notifications/initialized", "tools/call"]
    init = server.bodies[0]
    assert init["params"]["protocolVersion"] == "2025-06-18"
    assert init["params"]["clientInfo"] == {"name": "dsw-origination", "version": "0.1"}
    assert "id" not in server.bodies[1]
    for request in server.requests:
        assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
        assert request.headers["accept"] == "application/json, text/event-stream"
        assert request.headers["content-type"] == "application/json"
    assert "mcp-session-id" not in server.requests[0].headers
    assert server.requests[1].headers["mcp-session-id"] == "sess-abc"
    assert server.requests[2].headers["mcp-session-id"] == "sess-abc"


def test_session_is_reused_and_ids_increase(transport: McpTransport, server: FakeMcpServer) -> None:
    transport.call_tool("echo", {})
    transport.call_tool("echo", {})
    assert server.methods().count("initialize") == 1
    ids = [b["id"] for b in server.bodies if "id" in b]
    assert ids == sorted(set(ids))
    assert all(r.headers.get("mcp-session-id") == "sess-abc" for r in server.requests[1:])


def test_works_without_session_header(
    mocked: respx.MockRouter, server: FakeMcpServer, clock: FakeClock, http_client: httpx.Client
) -> None:
    server.session_id = None
    transport = McpTransport(FAKE_KEY, http_client=http_client, sleep=clock.sleep, monotonic=clock.monotonic)
    assert transport.call_tool("echo", {"x": "y"}) == {"got": {"x": "y"}}
    assert all("mcp-session-id" not in r.headers for r in server.requests)


def test_sse_responses_are_parsed(transport: McpTransport, server: FakeMcpServer) -> None:
    server.use_sse = True
    assert transport.call_tool("echo", {"q": 2}) == {"got": {"q": 2}}


def test_sse_detected_without_content_type(transport: McpTransport, server: FakeMcpServer) -> None:
    def handler(args: dict) -> httpx.Response:
        message = {"jsonrpc": "2.0", "id": server.bodies[-1]["id"], "result": tool_result({"ok": True})}
        return httpx.Response(
            200, text=": keepalive\n\n" + sse(message), headers={"content-type": "text/plain"}
        )

    server.tools["raw"] = handler
    assert transport.call_tool("raw", {}) == {"ok": True}


def test_parse_sse_handles_multiline_and_garbage() -> None:
    text = 'data: {"a":\ndata: 1}\n\ndata: not json\n\nevent: x\ndata: {"b": 2}'
    assert _parse_sse(text) == [{"a": 1}, {"b": 2}]


def test_tool_is_error_raises_tool_error_truncated(transport: McpTransport, server: FakeMcpServer) -> None:
    long_text = "Validation failed: " + "x" * 500
    server.tools["bad"] = lambda args: httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": server.bodies[-1]["id"],
            "result": tool_result(None, is_error=True, text=long_text),
        },
    )
    with pytest.raises(ToolError) as info:
        transport.call_tool("bad", {})
    assert info.value.retryable is False
    assert len(info.value.tool_message) <= 200
    assert "Validation failed" in str(info.value)
    assert_no_secret(info.value)


def test_jsonrpc_error_member_raises(transport: McpTransport, server: FakeMcpServer) -> None:
    server.tools["err"] = lambda args: httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": server.bodies[-1]["id"],
            "error": {"code": -32602, "message": "Bad params"},
        },
    )
    with pytest.raises(IntegrationError) as info:
        transport.call_tool("err", {})
    assert "-32602" in str(info.value)
    assert "Bad params" in str(info.value)
    assert not isinstance(info.value, ToolError)


def test_429_is_retried_respecting_retry_after(
    transport: McpTransport, server: FakeMcpServer, clock: FakeClock
) -> None:
    server.queued = [httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")]
    assert transport.call_tool("echo", {}) == {"got": {}}
    assert 7.0 in clock.sleeps
    assert server.methods().count("tools/call") == 2


def test_5xx_retried_with_backoff_then_gives_up(
    transport: McpTransport, server: FakeMcpServer, clock: FakeClock
) -> None:
    server.queued = [httpx.Response(503, text=f"upstream said {FAKE_KEY}") for _ in range(4)]
    with pytest.raises(IntegrationError) as info:
        transport.call_tool("echo", {})
    assert info.value.retryable is True
    assert "4 attempts" in str(info.value)
    assert server.methods().count("tools/call") == 4  # 1 try + 3 retries
    assert [s for s in clock.sleeps if s in (1.0, 2.0, 4.0)] == [1.0, 2.0, 4.0]
    assert_no_secret(info.value)


def test_401_is_not_retried(transport: McpTransport, server: FakeMcpServer) -> None:
    server.queued = [httpx.Response(401, text=f"invalid key {FAKE_KEY}")]
    with pytest.raises(IntegrationError) as info:
        transport.call_tool("echo", {})
    assert info.value.retryable is False
    assert "401" in str(info.value)
    assert server.methods().count("tools/call") == 1
    assert_no_secret(info.value)


def test_403_on_initialize_is_not_retried(
    mocked: respx.MockRouter, clock: FakeClock, http_client: httpx.Client
) -> None:
    mocked.routes.clear()
    route = mocked.post(MCP_URL).mock(return_value=httpx.Response(403))
    transport = McpTransport(FAKE_KEY, http_client=http_client, sleep=clock.sleep, monotonic=clock.monotonic)
    with pytest.raises(IntegrationError) as info:
        transport.call_tool("echo", {})
    assert info.value.retryable is False
    assert route.call_count == 1
    assert_no_secret(info.value)


def test_other_4xx_not_retried(transport: McpTransport, server: FakeMcpServer) -> None:
    server.queued = [httpx.Response(422, text="nope")]
    with pytest.raises(IntegrationError) as info:
        transport.call_tool("echo", {})
    assert info.value.retryable is False
    assert "nope" not in str(info.value)  # raw bodies never leak into messages


def test_timeout_is_retried_then_succeeds(transport: McpTransport, server: FakeMcpServer) -> None:
    server.queued = [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")]
    assert transport.call_tool("echo", {}) == {"got": {}}
    assert server.methods().count("tools/call") == 3


def test_persistent_timeout_raises_retryable(transport: McpTransport, server: FakeMcpServer) -> None:
    server.queued = [httpx.ReadTimeout("slow") for _ in range(4)]
    with pytest.raises(IntegrationError) as info:
        transport.call_tool("echo", {})
    assert info.value.retryable is True
    assert "timed out" in str(info.value)


def test_every_request_has_a_timeout(transport: McpTransport, mocked: respx.MockRouter) -> None:
    transport.call_tool("echo", {})
    for call in mocked.calls:
        timeouts = call.request.extensions["timeout"]
        assert all(v is not None and v > 0 for v in timeouts.values())


def test_throttle_uses_injected_sleep(transport: McpTransport, clock: FakeClock) -> None:
    transport.call_tool("echo", {})
    transport.call_tool("echo", {})
    # 4 requests: init, notification, call, call -> three throttle waits of ~1.05s
    assert len(clock.sleeps) == 3
    assert all(abs(s - 1.05) < 1e-9 for s in clock.sleeps)


def test_throttle_skips_sleep_when_enough_time_passed(transport: McpTransport, clock: FakeClock) -> None:
    transport.call_tool("echo", {})
    clock.sleeps.clear()
    clock.now += 10
    transport.call_tool("echo", {})
    assert clock.sleeps == []


def test_session_error_triggers_single_reinitialise(transport: McpTransport, server: FakeMcpServer) -> None:
    transport.call_tool("echo", {})
    server.session_id = "sess-new"
    server.queued = [httpx.Response(404, json={"error": "Session not found"})]
    assert transport.call_tool("echo", {"n": 1}) == {"got": {"n": 1}}
    assert server.methods().count("initialize") == 2
    assert server.requests[-1].headers["mcp-session-id"] == "sess-new"


def test_session_error_twice_gives_up(transport: McpTransport, server: FakeMcpServer) -> None:
    server.queued = [
        httpx.Response(400, text="Bad Request: No valid session ID provided"),
        httpx.Response(400, text="Bad Request: No valid session ID provided"),
    ]
    with pytest.raises(IntegrationError) as info:
        transport.call_tool("echo", {})
    assert "session" in str(info.value).lower()
    assert server.methods().count("initialize") == 2


def test_400_without_session_mention_is_plain_error(transport: McpTransport, server: FakeMcpServer) -> None:
    server.queued = [httpx.Response(400, text="bad input")]
    with pytest.raises(IntegrationError):
        transport.call_tool("echo", {})
    assert server.methods().count("initialize") == 1


def test_initialize_rejected_with_session_error(
    mocked: respx.MockRouter, clock: FakeClock, http_client: httpx.Client
) -> None:
    mocked.routes.clear()
    mocked.post(MCP_URL).mock(return_value=httpx.Response(404, text="session unknown"))
    transport = McpTransport(FAKE_KEY, http_client=http_client, sleep=clock.sleep, monotonic=clock.monotonic)
    with pytest.raises(IntegrationError, match="initialisation"):
        transport.call_tool("echo", {})


def test_missing_api_key_fails_fast_without_network(
    mocked: respx.MockRouter, clock: FakeClock, http_client: httpx.Client
) -> None:
    transport = McpTransport("", http_client=http_client, sleep=clock.sleep, monotonic=clock.monotonic)
    with pytest.raises(IntegrationError) as info:
        transport.call_tool("echo", {})
    assert info.value.retryable is False
    assert not mocked.calls


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 999, "method": "ping"}),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 3, "result": "not-a-dict"}),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 3, "result": {"content": [{"text": "not json"}]}}),
    ],
)
def test_malformed_responses_raise(
    transport: McpTransport, server: FakeMcpServer, response: httpx.Response
) -> None:
    server.queued = [response]
    with pytest.raises(IntegrationError):
        transport.call_tool("echo", {})


def test_structured_content_fallback(transport: McpTransport, server: FakeMcpServer) -> None:
    server.queued = [
        httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 3, "result": {"content": [], "structuredContent": {"k": 1}}}
        )
    ]
    assert transport.call_tool("echo", {}) == {"k": 1}


def test_batched_json_array_response(transport: McpTransport, server: FakeMcpServer) -> None:
    server.queued = [
        httpx.Response(200, text=json.dumps([{"jsonrpc": "2.0", "id": 3, "result": tool_result([1, 2])}]))
    ]
    assert transport.call_tool("echo", {}) == [1, 2]


def test_repr_does_not_leak_and_injected_client_is_not_closed(http_client: httpx.Client) -> None:
    transport = McpTransport(FAKE_KEY, http_client=http_client)
    assert FAKE_KEY not in repr(transport)
    transport.close()
    assert not http_client.is_closed


def test_owned_client_has_timeout_and_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict] = []

    class StubClient:
        closed = False

        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def close(self) -> None:
            StubClient.closed = True

    monkeypatch.setattr(transport_module.httpx, "Client", StubClient)
    transport = McpTransport(FAKE_KEY)
    transport.close()
    assert created == [{"timeout": 30.0, "follow_redirects": False}]
    assert StubClient.closed


def test_retry_after_parsing() -> None:
    assert _retry_after(httpx.Response(429)) is None
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "3"})) == 3.0
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "9999"})) == 60.0
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "garbage"})) is None
    past = _retry_after(httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))
    assert past == 0.0
