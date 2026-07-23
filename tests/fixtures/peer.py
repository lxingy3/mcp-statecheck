"""One controlled MCP peer with selectable fault modes."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp_statecheck.execution import ExecutionResult
    from mcp_statecheck.model import Action

INITIALIZE_RESULT = {
    "protocolVersion": "2025-11-25",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "controlled-peer", "version": "0.1"},
}


def _result(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


@dataclass
class PeerState:
    mode: str
    session_id: str = "fixture-session-sensitive"
    get_count: int = 0
    last_event_ids: list[str | None] = field(default_factory=list)
    session_ids: list[str | None] = field(default_factory=list)
    protocol_versions: list[str | None] = field(default_factory=list)
    delete_count: int = 0
    delete_session_ids: list[str | None] = field(default_factory=list)
    delete_protocol_versions: list[str | None] = field(default_factory=list)
    post_methods: list[str | None] = field(default_factory=list)
    pending_initialize: dict[str, Any] | None = None
    duplicate_calls: list[dict[str, Any]] = field(default_factory=list)
    cancelled_call: dict[str, Any] | None = None
    cancellation_received: bool = False

    def handle_stdio(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            if self.mode == "request-before-initialized":
                self.pending_initialize = message
                return []
            return [_result(request_id, INITIALIZE_RESULT)]

        if method == "notifications/initialized":
            return []

        if method == "tools/list":
            response = _result(request_id, {"tools": []})
            if self.mode == "unknown-response-id":
                return [_result(999, {}), response]
            if self.mode == "invalid-json-rpc-error":
                return [{"jsonrpc": "2.0", "id": request_id, "error": "broken"}]
            if self.pending_initialize is not None:
                initialize = _result(self.pending_initialize["id"], INITIALIZE_RESULT)
                self.pending_initialize = None
                return [response, initialize]
            return [response]

        if method == "tools/call" and self.mode == "duplicate-concurrent-request-id":
            self.duplicate_calls.append(message)
            if len(self.duplicate_calls) < 2:
                return []
            first, second = self.duplicate_calls
            self.duplicate_calls.clear()
            return [
                _result(second["id"], {"which": second["params"]["label"]}),
                _result(first["id"], {"which": first["params"]["label"]}),
            ]

        if method == "tools/call" and self.mode == "late-response-after-cancellation":
            if self.cancelled_call is None:
                self.cancelled_call = message
                return []
            if self.cancellation_received:
                cancelled = self.cancelled_call
                self.cancelled_call = None
                self.cancellation_received = False
                cancelled_canary = cancelled["params"]["arguments"]["fixtureCanary"]
                current_canary = message["params"]["arguments"]["fixtureCanary"]
                return [
                    _result(
                        request_id,
                        {
                            "content": [],
                            "structuredContent": {"fixtureCanary": cancelled_canary},
                        },
                    ),
                    _result(
                        cancelled["id"],
                        {
                            "content": [],
                            "structuredContent": {"fixtureCanary": current_canary},
                        },
                    ),
                ]
            return []

        if method == "notifications/cancelled" and self.cancelled_call is not None:
            params = message.get("params")
            cancelled_id = self.cancelled_call.get("id")
            if (
                isinstance(params, dict)
                and type(params.get("requestId")) is type(cancelled_id)
                and params.get("requestId") == cancelled_id
            ):
                self.cancellation_received = True
            return []

        if method == "ping":
            return [_result(request_id, {})]

        return [_result(request_id, {})] if "id" in message else []


def run_stdio(mode: str) -> int:
    state = PeerState(mode)
    for raw_line in sys.stdin:
        try:
            message = json.loads(raw_line)
            replies = state.handle_stdio(message)
        except Exception as exc:
            print(f"fixture input error: {exc}", file=sys.stderr, flush=True)
            return 2
        for reply in replies:
            print(json.dumps(reply, separators=(",", ":")), flush=True)
    return 0


class ControlledHTTPPeer(AbstractContextManager["ControlledHTTPPeer"]):
    def __init__(self, mode: str) -> None:
        self.state = PeerState(mode)
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_: object) -> None:
                return

            def _send(
                self,
                status: int,
                body: bytes = b"",
                content_type: str = "application/json",
                headers: dict[str, str] | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                message = json.loads(self.rfile.read(length))
                state.post_methods.append(message.get("method"))
                if state.mode == "http-error-as-timeout":
                    body = json.dumps({"error": "controlled outage"}).encode()
                    self._send(503, body)
                    return
                if state.mode == "initialize-error":
                    body = json.dumps(
                        {
                            "error": {
                                "code": -32603,
                                "message": "controlled initialize failure",
                            },
                            "id": message.get("id"),
                            "jsonrpc": "2.0",
                        }
                    ).encode()
                    self._send(200, body)
                    return
                if "id" not in message:
                    self._send(202)
                    return
                result = (
                    INITIALIZE_RESULT if message.get("method") == "initialize" else {}
                )
                body = json.dumps(_result(message.get("id"), result)).encode()
                headers = (
                    {"MCP-Session-Id": state.session_id}
                    if message.get("method") == "initialize"
                    else {}
                )
                self._send(200, body, headers=headers)

            def do_GET(self) -> None:  # noqa: N802
                state.last_event_ids.append(self.headers.get("Last-Event-ID"))
                state.session_ids.append(self.headers.get("MCP-Session-Id"))
                state.protocol_versions.append(self.headers.get("MCP-Protocol-Version"))
                state.get_count += 1
                cursor = (
                    "cursor-é"
                    if state.mode == "unicode-sse-cursor" and state.get_count == 1
                    else f"cursor-{state.get_count}"
                )
                data = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/message",
                        "params": {"sequence": state.get_count},
                    },
                    separators=(",", ":"),
                )
                body = f"id: {cursor}\nretry: 0\ndata: {data}\n\n".encode()
                self._send(200, body, "text/event-stream")

            def do_DELETE(self) -> None:  # noqa: N802
                state.delete_count += 1
                state.delete_session_ids.append(self.headers.get("MCP-Session-Id"))
                state.delete_protocol_versions.append(
                    self.headers.get("MCP-Protocol-Version")
                )
                self._send(200)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True
        self.host, self.port = self._server.server_address
        self.url = f"http://{self.host}:{self.port}/mcp"

    def __enter__(self) -> "ControlledHTTPPeer":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise RuntimeError("fixture HTTP thread did not stop")
        probe = socket.socket()
        try:
            # Linux keeps recently closed connections in TIME_WAIT. Matching
            # the server's reuse policy distinguishes that state from a live
            # listener while still permitting an immediate bind check.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((self.host, self.port))
        finally:
            probe.close()


async def execute_controlled_http_fault(
    actions: Sequence[Action],
    fixture_id: str,
    timeout: float,
) -> ExecutionResult:
    """Run one real HTTP peer while injecting a test-only adapter fault."""

    from mcp_statecheck.execution import (
        ExecutionProtocolError,
        execute_http,
    )
    from mcp_statecheck.model import ActionKind

    if fixture_id not in {
        "http-error-as-timeout",
        "second-sse-resume-token-loss",
    }:
        raise ValueError(f"unsupported controlled HTTP fixture: {fixture_id}")

    wire_actions = tuple(actions)
    if fixture_id == "second-sse-resume-token-loss":
        resume_count = 0
        rewritten: list[Action] = []
        for action in wire_actions:
            if action.kind is ActionKind.RESUME_STREAM:
                resume_count += 1
                if resume_count == 2:
                    action = replace(action, resume_token="")
            rewritten.append(action)
        wire_actions = tuple(rewritten)

    with ControlledHTTPPeer(fixture_id) as peer:
        result = await execute_http(wire_actions, peer.url, timeout=timeout)
        events = [dict(event) for event in result.events]
        if fixture_id == "http-error-as-timeout":
            transformed = False
            for index, event in enumerate(events):
                if event.get("kind") == "http_error" and event.get("status") == 503:
                    events[index] = {
                        "fixture_source_kind": "http_error",
                        "http_method": event.get("http_method"),
                        "kind": "timeout",
                        "status": 503,
                        "target_action_id": event.get("target_action_id"),
                    }
                    transformed = True
            if (
                any(action.kind is ActionKind.INITIALIZE for action in wire_actions)
                and not transformed
            ):
                raise ExecutionProtocolError(
                    "controlled HTTP error fixture did not observe status 503"
                )
        else:
            sse_indexes = [
                index
                for index, event in enumerate(events)
                if event.get("kind") == "sse_resume"
            ]
            if len(sse_indexes) != len(peer.state.last_event_ids):
                raise ExecutionProtocolError(
                    "controlled peer and adapter observed different SSE request counts"
                )
            for index, observed in zip(
                sse_indexes,
                peer.state.last_event_ids,
                strict=True,
            ):
                events[index]["peer_last_event_id"] = observed
            for index, session_id, protocol_version in zip(
                sse_indexes,
                peer.state.session_ids,
                peer.state.protocol_versions,
                strict=True,
            ):
                events[index]["peer_session_id"] = session_id
                events[index]["peer_protocol_version"] = protocol_version
            if peer.state.session_ids != [peer.state.session_id] * len(sse_indexes):
                raise ExecutionProtocolError(
                    "controlled SSE fixture did not preserve its HTTP session"
                )
            if any(version != "2025-11-25" for version in peer.state.protocol_versions):
                raise ExecutionProtocolError(
                    "controlled SSE fixture lost its protocol version"
                )
            if len(sse_indexes) >= 3:
                if (
                    peer.state.delete_count != 1
                    or peer.state.delete_session_ids != [peer.state.session_id]
                    or peer.state.delete_protocol_versions != ["2025-11-25"]
                ):
                    raise ExecutionProtocolError(
                        "controlled SSE fixture did not delete its HTTP session"
                    )

    return replace(
        result,
        events=tuple(events),
        cleanup={
            **result.cleanup,
            "listener_closed": True,
            **(
                {"session_deleted": peer.state.delete_count == 1}
                if fixture_id == "second-sse-resume-token-loss"
                else {}
            ),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--mode", required=True)
    args = parser.parse_args()
    if not args.stdio:
        parser.error("only --stdio is available from the command line")
    return run_stdio(args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
