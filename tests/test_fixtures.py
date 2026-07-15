from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

from mcp_statecheck.trace import TraceRecorder
from mcp_statecheck.transports import (
    ServerError,
    StdioTransport,
    StreamableHTTPTransport,
)
from tests.fixtures.peer import ControlledHTTPPeer

FIXTURE_PEER = Path(__file__).parent / "fixtures" / "peer.py"
PROTOCOL_VERSION = "2025-11-25"


def _initialize(request_id: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "m1-fixture", "version": "0.1"},
        },
    }


def _stdio_command(mode: str) -> list[str]:
    return [
        sys.executable,
        str(FIXTURE_PEER),
        "--stdio",
        "--mode",
        mode,
    ]


def _trace(
    fixture_id: str,
    transport: str,
    actions: list[dict[str, Any]],
    events: list[dict[str, Any]],
    cleanup: dict[str, bool],
) -> dict[str, Any]:
    recorder = TraceRecorder(
        protocol_version=PROTOCOL_VERSION,
        adapter="wire",
        sdk_version="none",
        transport=transport,
        seed=0,
        fixture_id=fixture_id,
        cleanup=cleanup,
    )
    for action in actions:
        recorder.record_action(action)
    for event in events:
        recorder.record_event(event)
    output_dir = os.environ.get("MCP_STATECHECK_ARTIFACT_DIR")
    if output_dir:
        recorder.write(Path(output_dir) / f"{fixture_id}.json")
    return recorder.artifact()


def test_http_error_is_not_normalized_as_timeout() -> None:
    async def run() -> None:
        with ControlledHTTPPeer("http-error-as-timeout") as peer:
            async with StreamableHTTPTransport(peer.url, timeout=1) as transport:
                with pytest.raises(ServerError) as raised:
                    await transport.send(_initialize())
                assert raised.value.status_code == 503

        artifact = _trace(
            "http-error-as-timeout",
            "streamable-http",
            [{"action_id": "initialize", "kind": "send", "method": "initialize"}],
            [{"kind": "http_error", "status": raised.value.status_code}],
            {"client_closed": True, "listener_closed": True},
        )
        assert artifact["normalized_events"][0]["kind"] == "http_error"

    anyio.run(run)


def test_duplicate_request_ids_remain_two_logical_actions() -> None:
    async def run() -> None:
        transport = StdioTransport(
            _stdio_command("duplicate-concurrent-request-id"), timeout=1
        )
        async with transport:
            await transport.send(_initialize())
            await transport.receive()
            await transport.send(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            for label in ("first", "second"):
                await transport.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {"name": "fixture", "label": label},
                    }
                )
            replies = [await transport.receive(), await transport.receive()]

        assert [reply["id"] for reply in replies] == [7, 7]
        assert [reply["result"]["which"] for reply in replies] == ["second", "first"]
        assert transport.returncode == 0
        artifact = _trace(
            "duplicate-concurrent-request-id",
            "stdio",
            [
                {"action_id": "initialize", "kind": "initialize"},
                {"action_id": "initialized", "kind": "initialized"},
                {"action_id": "call-a", "kind": "request", "mcp_request_id": 7},
                {"action_id": "call-b", "kind": "request", "mcp_request_id": 7},
            ],
            [
                {"kind": "response", "mcp_request_id": 7, "which": "second"},
                {"kind": "response", "mcp_request_id": 7, "which": "first"},
            ],
            {"child_reaped": transport.returncode is not None},
        )
        ids = [
            action["action_id"]
            for action in artifact["canonical_actions"]
            if action.get("mcp_request_id") == 7
        ]
        assert ids == ["call-a", "call-b"]

    anyio.run(run)


def test_second_sse_reconnect_uses_latest_event_id() -> None:
    async def run() -> None:
        with ControlledHTTPPeer("second-sse-resume-token-loss") as peer:
            async with StreamableHTTPTransport(peer.url, timeout=1) as transport:
                await transport.send(_initialize())
                transport.set_protocol_version(PROTOCOL_VERSION)
                messages = []
                for _ in range(3):
                    messages.extend(await transport.resume("server-events"))
                final_cursor = transport.cursor("server-events")
                session_id = transport.session_id

            observed_ids = peer.state.last_event_ids

        assert observed_ids == [None, "cursor-1", "cursor-2"]
        assert final_cursor == "cursor-3"
        assert [message["params"]["sequence"] for message in messages] == [1, 2, 3]
        artifact = _trace(
            "second-sse-resume-token-loss",
            "streamable-http",
            [
                {"action_id": "initialize", "kind": "initialize"},
                {"action_id": "open-sse", "kind": "open_stream"},
                {"action_id": "resume-1", "kind": "resume", "cursor": "cursor-1"},
                {"action_id": "resume-2", "kind": "resume", "cursor": "cursor-2"},
            ],
            [
                {
                    "kind": "sse_sequence",
                    "last_event_ids": observed_ids,
                    "final_cursor": final_cursor,
                    "session_id": session_id,
                }
            ],
            {"client_closed": True, "listener_closed": True},
        )
        serialized = str(artifact)
        assert "fixture-session-sensitive" not in serialized
        assert "[SESSION_1]" in serialized

    anyio.run(run)


def test_peer_accepts_request_before_initialize_result() -> None:
    async def run() -> None:
        transport = StdioTransport(
            _stdio_command("request-before-initialized"), timeout=1
        )
        async with transport:
            await transport.send(_initialize(1))
            await transport.send(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            )
            first = await transport.receive()
            second = await transport.receive()

        assert [first["id"], second["id"]] == [2, 1]
        artifact = _trace(
            "request-before-initialized",
            "stdio",
            [
                {"action_id": "initialize-pending", "kind": "initialize"},
                {"action_id": "tools-list", "kind": "request", "method": "tools/list"},
            ],
            [
                {"kind": "response", "mcp_request_id": first["id"]},
                {"kind": "response", "mcp_request_id": second["id"]},
            ],
            {"child_reaped": transport.returncode is not None},
        )
        assert artifact["normalized_events"][0]["mcp_request_id"] == 2

    anyio.run(run)


def test_late_cancelled_response_does_not_consume_next_call() -> None:
    async def run() -> None:
        transport = StdioTransport(
            _stdio_command("late-response-after-cancellation"), timeout=1
        )
        async with transport:
            await transport.send(_initialize())
            await transport.receive()
            await transport.send(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            await transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": 21,
                    "method": "tools/call",
                    "params": {"name": "fixture", "label": "cancelled"},
                }
            )
            await transport.send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 21, "reason": "fixture"},
                }
            )
            late = await transport.receive()
            await transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": 22,
                    "method": "tools/call",
                    "params": {"name": "fixture", "label": "next"},
                }
            )
            current = await transport.receive()

        assert (late["id"], late["result"]["which"]) == (21, "cancelled")
        assert (current["id"], current["result"]["which"]) == (22, "next")
        artifact = _trace(
            "late-response-after-cancellation",
            "stdio",
            [
                {"action_id": "initialize", "kind": "initialize"},
                {"action_id": "initialized", "kind": "initialized"},
                {"action_id": "call-a", "kind": "request", "mcp_request_id": 21},
                {"action_id": "cancel-a", "kind": "cancel", "target": "call-a"},
                {"action_id": "call-b", "kind": "request", "mcp_request_id": 22},
            ],
            [
                {"kind": "late_response", "target_action_id": "call-a"},
                {"kind": "response", "target_action_id": "call-b"},
            ],
            {"child_reaped": transport.returncode is not None},
        )
        assert artifact["normalized_events"][0]["kind"] == "late_response"

    anyio.run(run)
