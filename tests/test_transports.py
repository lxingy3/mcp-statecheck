from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import httpx
import pytest

from mcp_statecheck.transports import (
    Forbidden,
    HTTPProtocolError,
    HTTPTimeout,
    HTTPTransportError,
    ServerError,
    SessionExpired,
    SSEEvent,
    SSEParser,
    StdioTimeout,
    StdioTransport,
    StreamableHTTPTransport,
    Unauthorized,
)
from tests.fixtures.peer import INITIALIZE_RESULT, ControlledHTTPPeer


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


class TimeoutAfterHeaders(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'{"jsonrpc":"2.0"'
        raise httpx.ReadTimeout("body stalled")


def run(async_fn: object) -> object:
    return anyio.run(async_fn)  # type: ignore[arg-type]


def test_stdio_uses_argv_json_lines_and_bounded_stderr() -> None:
    async def scenario() -> None:
        script = (
            "import json,sys; "
            "sys.stderr.write('x'*200000); sys.stderr.flush(); "
            "value=json.loads(sys.stdin.readline()); "
            "print(json.dumps(value), flush=True)"
        )
        transport = StdioTransport(
            [sys.executable, "-c", script], stderr_limit=128, timeout=5
        )
        async with transport:
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert await transport.receive() == {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "ping",
            }
        assert transport.returncode is not None
        assert len(transport.stderr.removeprefix("[truncated]\n")) == 128

    run(scenario)


def test_stdio_rejects_shell_strings_and_reaps_on_timeout() -> None:
    with pytest.raises(TypeError):
        StdioTransport(f'{sys.executable} -c "pass"')

    async def scenario() -> None:
        transport = StdioTransport(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.1,
            shutdown_timeout=0.2,
        )
        await transport.start()
        with pytest.raises(StdioTimeout):
            await transport.receive()
        assert transport.returncode is not None

    run(scenario)


def test_transport_deadlines_must_be_finite() -> None:
    for timeout in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            StdioTransport([sys.executable, "-c", "pass"], timeout=timeout)
        with pytest.raises(ValueError, match="finite"):
            StdioTransport(
                [sys.executable, "-c", "pass"],
                shutdown_timeout=timeout,
            )
        with pytest.raises(ValueError, match="finite"):
            StreamableHTTPTransport("https://example.invalid/mcp", timeout=timeout)


def test_stdio_supports_cwd_and_copies_environment(tmp_path: Path) -> None:
    async def scenario() -> None:
        script = (
            "import json,os,sys; "
            "value=json.loads(sys.stdin.readline()); "
            "value.update(cwd=os.getcwd(), marker=os.environ['STATECHECK_MARKER']); "
            "print(json.dumps(value), flush=True)"
        )
        env = dict(os.environ, STATECHECK_MARKER="before")
        transport = StdioTransport(
            [sys.executable, "-c", script], cwd=tmp_path, env=env
        )
        env["STATECHECK_MARKER"] = "after"
        async with transport:
            await transport.send({"newline": "preserved\nvalue"})
            response = await transport.receive()
        assert response == {
            "newline": "preserved\nvalue",
            "cwd": os.fspath(tmp_path),
            "marker": "before",
        }

    run(scenario)


def test_sse_parser_handles_chunks_lines_and_fields() -> None:
    parser = SSEParser()
    assert parser.feed(b": keepalive\r\nid: cursor-") == []
    assert parser.feed(b"1\r") == []
    events = parser.feed(b"\nevent: update\nretry: 1500\ndata: first\ndata: second\n\n")
    assert events == [
        type(events[0])(
            data="first\nsecond",
            event="update",
            event_id="cursor-1",
            retry=1500,
        )
    ]
    assert parser.last_event_id == "cursor-1"


def test_sse_parser_ignores_a_split_leading_utf8_bom() -> None:
    parser = SSEParser()

    assert parser.feed(b"\xef") == []
    assert parser.feed(b"\xbb") == []
    assert parser.feed(b'\xbfid: cursor-1\ndata: {"jsonrpc":"2.0"}\n\n') == [
        SSEEvent(data='{"jsonrpc":"2.0"}', event_id="cursor-1")
    ]
    assert parser.last_event_id == "cursor-1"


def test_sse_parser_rejects_retry_values_outside_its_numeric_boundary() -> None:
    parser = SSEParser()

    with pytest.raises(HTTPProtocolError, match="retry value"):
        parser.feed(b"retry: " + b"9" * 5000 + b"\n\n")


def test_http_sse_enforces_total_byte_limit() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=ChunkStream(b": a\n\n", b": b\n\n", b": c\n\n"),
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp",
            transport=httpx.MockTransport(handler),
            max_response_bytes=10,
        )
        with pytest.raises(HTTPProtocolError):
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        await transport.close()

    run(scenario)


def test_http_preserves_sse_retry_per_stream() -> None:
    retries = iter([25, 0])

    async def handler(_: httpx.Request) -> httpx.Response:
        retry = next(retries)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=(
                f'retry: {retry}\ndata: {{"jsonrpc":"2.0","method":"tick"}}\n\n'
            ).encode(),
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        await transport.send(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"}, stream="alpha"
        )
        await transport.send(
            {"jsonrpc": "2.0", "id": 2, "method": "ping"}, stream="beta"
        )
        assert transport.retry("alpha") == 25
        assert transport.retry("beta") == 0
        await transport.close()

    run(scenario)


def test_http_resume_retry_delay_is_inside_hard_timeout() -> None:
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b'retry: 1000\ndata: {"jsonrpc":"2.0","method":"tick"}\n\n',
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp",
            transport=httpx.MockTransport(handler),
            timeout=0.05,
        )
        await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        with pytest.raises(HTTPTimeout):
            await transport.resume()
        assert methods == ["POST"]
        await transport.close()

    run(scenario)


def test_http_session_headers_sse_resume_and_delete() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["accept"] == "application/json, text/event-stream"
        index = len(requests)
        if index == 1:
            assert request.method == "POST"
            assert "mcp-session-id" not in request.headers
            assert "mcp-protocol-version" not in request.headers
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "MCP-Session-Id": "session-1",
                },
                json={"jsonrpc": "2.0", "id": 1, "result": INITIALIZE_RESULT},
            )
        assert request.headers["mcp-session-id"] == "session-1"
        assert request.headers["mcp-protocol-version"] == "2025-11-25"
        if index == 2:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=ChunkStream(
                    b"id: cursor-",
                    b'1\ndata: {"jsonrpc":"2.0","id":2,',
                    b'"result":{}}\n\n',
                ),
            )
        if index == 3:
            assert request.method == "GET"
            assert request.headers["last-event-id"] == "cursor-1"
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=b'id: cursor-2\ndata: {"jsonrpc":"2.0","method":"one"}\n\n',
            )
        if index == 4:
            assert request.method == "GET"
            assert request.headers["last-event-id"] == "cursor-2"
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=b'id: cursor-3\ndata: {"jsonrpc":"2.0","method":"two"}\n\n',
            )
        assert request.method == "DELETE"
        return httpx.Response(200)

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        transport.session_id = "stale-session"
        initialized = await transport.send(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )
        assert initialized[0]["id"] == 1
        assert transport.session_id == "session-1"
        transport.set_protocol_version("2025-11-25")
        await transport.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert transport.cursor() == "cursor-1"
        await transport.resume()
        assert transport.cursor() == "cursor-2"
        await transport.resume()
        assert transport.cursor() == "cursor-3"
        await transport.close()

    run(scenario)
    assert [request.method for request in requests] == [
        "POST",
        "POST",
        "GET",
        "GET",
        "DELETE",
    ]


def test_http_reconnect_encodes_unicode_event_id_as_utf8_header_bytes() -> None:
    observed: list[bytes | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(
            next(
                (
                    value
                    for name, value in request.headers.raw
                    if name.lower() == b"last-event-id"
                ),
                None,
            )
        )
        sequence = len(observed)
        event_id = "cursor-é" if sequence == 1 else "cursor-final"
        body = (
            f'id: {event_id}\ndata: {{"jsonrpc":"2.0","method":"tick-{sequence}"}}\n\n'
        ).encode()
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=body,
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        assert await transport.resume() == [{"jsonrpc": "2.0", "method": "tick-1"}]
        assert await transport.resume() == [{"jsonrpc": "2.0", "method": "tick-2"}]
        await transport.close()

    run(scenario)
    assert observed == [None, "cursor-é".encode()]


def test_http_reconnect_sends_unicode_event_id_over_a_real_socket() -> None:
    async def scenario() -> list[str | None]:
        with ControlledHTTPPeer("unicode-sse-cursor") as peer:
            async with StreamableHTTPTransport(peer.url, timeout=1) as transport:
                await transport.resume()
                await transport.resume()
            return peer.state.last_event_ids

    observed = anyio.run(scenario)
    assert observed[0] is None
    assert observed[1] is not None
    assert observed[1].encode("latin-1") == "cursor-é".encode()


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, Unauthorized), (403, Forbidden), (503, ServerError)],
)
def test_http_statuses_are_not_timeouts(
    status: int, error_type: type[Exception]
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(error_type):
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        await transport.close()

    run(scenario)


def test_http_timeout_is_explicit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("late", request=request)

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(HTTPTimeout):
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        await transport.close()

    run(scenario)


def test_http_body_timeout_keeps_received_status() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=TimeoutAfterHeaders(),
        )

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(HTTPTimeout) as caught:
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert caught.value.status_code == 200
        await transport.close()

    run(scenario)


def test_http_transport_error_is_public_base_class() -> None:
    assert issubclass(HTTPTimeout, HTTPTransportError)


def test_session_404_clears_session_without_reinitializing() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "MCP-Session-Id": "old-session",
                },
                json={"jsonrpc": "2.0", "id": 1, "result": INITIALIZE_RESULT},
            )
        return httpx.Response(404)

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        await transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        with pytest.raises(SessionExpired) as caught:
            await transport.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert caught.value.session_id == "old-session"
        assert "old-session" not in str(caught.value)
        assert transport.session_id is None
        assert calls == 2
        await transport.close()

    run(scenario)


@pytest.mark.parametrize("status", [404, 405])
def test_http_delete_missing_or_unsupported_is_successful_cleanup(status: int) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.headers["mcp-session-id"] == "session-to-close"
        return httpx.Response(status)

    async def scenario() -> None:
        transport = StreamableHTTPTransport(
            "https://example.test/mcp", transport=httpx.MockTransport(handler)
        )
        transport.session_id = "session-to-close"
        await transport.close()
        assert transport.session_id is None

    run(scenario)


@pytest.mark.parametrize("session_id", ["", "contains space"])
def test_http_rejects_non_visible_ascii_session_id(session_id: str) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "MCP-Session-Id": session_id,
            },
            json={"jsonrpc": "2.0", "id": 1, "result": INITIALIZE_RESULT},
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
