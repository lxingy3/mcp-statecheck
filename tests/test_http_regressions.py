from __future__ import annotations

import math
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest

from mcp_statecheck.transports import (
    HTTPProtocolError,
    HTTPTimeout,
    HTTPTransportError,
    StreamableHTTPTransport,
)
from tests.fixtures.peer import INITIALIZE_RESULT


class OneThenStall(httpx.AsyncByteStream):
    def __init__(self, chunk: bytes) -> None:
        self.chunk = chunk
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.chunk
        await anyio.sleep_forever()

    async def aclose(self) -> None:
        self.closed = True


class Stall(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        await anyio.sleep_forever()
        yield b""


class CloseProbe(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.closed = False

    async def handle_async_request(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    async def aclose(self) -> None:
        await anyio.sleep(0)
        self.closed = True


def run(async_fn: object) -> object:
    return anyio.run(async_fn)  # type: ignore[arg-type]


def test_initialize_commits_session_only_after_matching_result() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "MCP-Session-Id": "not-committed",
            },
            json={"jsonrpc": "2.0", "id": 2, "result": INITIALIZE_RESULT},
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(HTTPProtocolError):
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert transport.session_id is None
        await transport.close()

    run(scenario)


def test_initialize_rejects_result_without_initialize_result_fields() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "MCP-Session-Id": "not-committed",
            },
            json={"jsonrpc": "2.0", "id": 1, "result": {}},
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(HTTPProtocolError):
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert transport.session_id is None
        await transport.close()

    run(scenario)


def test_failed_initialize_does_not_commit_session() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "MCP-Session-Id": "not-committed",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32602, "message": "unsupported"},
            },
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        messages = await transport.send(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )
        assert "error" in messages[0]
        assert transport.session_id is None
        await transport.close()

    run(scenario)


def test_initialize_body_timeout_keeps_status_and_discards_session() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "MCP-Session-Id": "not-committed",
            },
            stream=Stall(),
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp",
            transport=httpx.MockTransport(handler),
            timeout=0.02,
        )
        with pytest.raises(HTTPTimeout) as caught:
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert caught.value.status_code == 200
        assert transport.session_id is None
        await transport.close()

    run(scenario)


@pytest.mark.parametrize(
    ("message", "status"),
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "ping"}, 201),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping"}, 202),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping"}, 204),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, 200),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, 201),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, 204),
        ({"jsonrpc": "2.0", "id": 1, "result": {}}, 200),
    ],
)
def test_post_rejects_wrong_success_status(
    message: dict[str, Any], status: int
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"Content-Type": "application/json"})

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(HTTPProtocolError):
            await transport.send(message)
        await transport.close()

    run(scenario)


def test_accepted_notification_requires_empty_body() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(202, content=b"unexpected")

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(HTTPProtocolError):
            await transport.send(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
        await transport.close()

    run(scenario)


@pytest.mark.parametrize(
    "body",
    [
        b'{"jsonrpc":"2.0","id":1,"id":1,"result":{}}',
        b'{"jsonrpc":"2.0","id":1,"result":{"value":NaN}}',
        b'{"jsonrpc":"2.0","id":1,"result":{"value":Infinity}}',
    ],
)
def test_json_response_rejects_nonstandard_json(body: bytes) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, content=body
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(HTTPProtocolError):
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        await transport.close()

    run(scenario)


def test_sse_rejects_duplicate_keys() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b'data: {"jsonrpc":"2.0","id":1,"id":1,"result":{}}\n\n',
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(HTTPProtocolError):
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        await transport.close()

    run(scenario)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_outbound_json_rejects_nonfinite_numbers(value: float) -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(202)

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(HTTPProtocolError):
            await transport.send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {"value": value},
                }
            )
        assert calls == 0
        await transport.close()

    run(scenario)


def test_post_sse_returns_at_matching_response_before_eof() -> None:
    body = OneThenStall(
        b"id: prime\ndata:\n\nid: response-1\n"
        b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=body,
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp",
            transport=httpx.MockTransport(handler),
            timeout=0.2,
        )
        messages = await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert messages == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
        assert transport.cursor() == "response-1"
        assert body.closed
        await transport.close()

    run(scenario)


def test_get_iterator_yields_before_eof_and_closes_when_stopped() -> None:
    body = OneThenStall(
        b'id: prime\ndata:\n\nid: event-1\ndata: {"jsonrpc":"2.0","method":"tick"}\n\n'
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=body,
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp",
            transport=httpx.MockTransport(handler),
            timeout=0.2,
        )
        messages = transport.iter_messages("events")
        with anyio.fail_after(0.1):
            assert await anext(messages) == {"jsonrpc": "2.0", "method": "tick"}
        assert transport.cursor("events") == "event-1"
        await messages.aclose()
        assert body.closed
        await transport.close()

    run(scenario)


def test_resume_returns_one_message_and_closes_long_lived_get() -> None:
    body = OneThenStall(b'data: {"jsonrpc":"2.0","method":"tick"}\n\n')

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=body,
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp",
            transport=httpx.MockTransport(handler),
            timeout=0.2,
        )
        assert await transport.resume() == [{"jsonrpc": "2.0", "method": "tick"}]
        assert body.closed
        await transport.close()

    run(scenario)


def test_incomplete_sse_id_is_not_committed_as_cursor() -> None:
    body = OneThenStall(b"id: uncommitted\n")

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=body,
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp",
            transport=httpx.MockTransport(handler),
            timeout=0.02,
        )
        with pytest.raises(HTTPTimeout):
            await transport.resume("events")
        assert transport.cursor("events") is None
        await transport.close()

    run(scenario)


def test_close_finishes_inside_cancelled_scope() -> None:
    async def scenario() -> None:
        probe = CloseProbe()
        transport = StreamableHTTPTransport("https://example.test/mcp", transport=probe)
        with anyio.CancelScope() as scope:
            scope.cancel()
            await transport.close()
        assert probe.closed
        with pytest.raises(HTTPTransportError, match="closed"):
            await transport.send(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )

    run(scenario)


def test_reserved_state_headers_cannot_be_injected() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "result": INITIALIZE_RESULT},
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b'data: {"jsonrpc":"2.0","method":"tick"}\n\n',
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp",
            headers={
                "MCP-Session-Id": "stale-session",
                "MCP-Protocol-Version": "stale-version",
                "Last-Event-ID": "stale-event",
            },
            transport=httpx.MockTransport(handler),
        )
        await transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        await transport.resume()
        await transport.close()

    run(scenario)
    assert "mcp-session-id" not in requests[0].headers
    assert "mcp-protocol-version" not in requests[0].headers
    assert "last-event-id" not in requests[1].headers
