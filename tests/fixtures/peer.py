"""One controlled MCP peer with selectable fault modes."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp_statecheck.execution import ExecutionResult
    from mcp_statecheck.model import Action


def _initialize_result(protocol_version: str) -> dict[str, Any]:
    return {
        "protocolVersion": protocol_version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "controlled-peer", "version": "0.1"},
    }


INITIALIZE_RESULT = _initialize_result("2025-11-25")
SDK_MODES = {"sdk-hang", "sdk-smoke"}
OBSERVED_STDIO_MODES = SDK_MODES | {
    "initialize-error",
    "initialize-invalid-result",
}


def _result(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


@dataclass
class PeerState:
    mode: str
    negotiated_protocol_version: str = "2025-11-25"
    session_id: str = "fixture-session-sensitive"
    get_count: int = 0
    last_event_ids: list[str | None] = field(default_factory=list)
    session_ids: list[str | None] = field(default_factory=list)
    protocol_versions: list[str | None] = field(default_factory=list)
    delete_count: int = 0
    delete_session_ids: list[str | None] = field(default_factory=list)
    delete_protocol_versions: list[str | None] = field(default_factory=list)
    delete_authorizations: list[str | None] = field(default_factory=list)
    post_methods: list[str | None] = field(default_factory=list)
    post_accepts: list[str | None] = field(default_factory=list)
    post_session_ids: list[str | None] = field(default_factory=list)
    post_protocol_versions: list[str | None] = field(default_factory=list)
    post_authorizations: list[str | None] = field(default_factory=list)
    pending_initialize: dict[str, Any] | None = None
    duplicate_calls: list[dict[str, Any]] = field(default_factory=list)
    cancelled_call: dict[str, Any] | None = None
    cancellation_received: bool = False
    server_ping_responses: list[dict[str, Any]] = field(default_factory=list)
    stdio_methods: list[str | None] = field(default_factory=list)
    initialize_protocol_versions: list[str | None] = field(default_factory=list)

    def handle_stdio(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        method = message.get("method")
        request_id = message.get("id")
        if (
            self.mode == "server-ping-before-response"
            and method is None
            and request_id == "server-ping"
        ):
            self.server_ping_responses.append(message)
            return []
        if self.mode in OBSERVED_STDIO_MODES:
            self.stdio_methods.append(method)

        if method == "initialize":
            if self.mode in OBSERVED_STDIO_MODES:
                params = message.get("params")
                self.initialize_protocol_versions.append(
                    params.get("protocolVersion") if isinstance(params, dict) else None
                )
            if self.mode == "initialize-error":
                return [
                    {
                        "error": {
                            "code": -32603,
                            "message": "controlled initialize failure",
                        },
                        "id": request_id,
                        "jsonrpc": "2.0",
                    }
                ]
            if self.mode == "initialize-invalid-result":
                return [_result(request_id, {})]
            if self.mode == "request-before-initialized":
                self.pending_initialize = message
                return []
            return [
                _result(
                    request_id,
                    _initialize_result(self.negotiated_protocol_version),
                )
            ]

        if method == "notifications/initialized":
            return []

        if method == "tools/list":
            tools = (
                [
                    {
                        "name": "echo",
                        "description": "Return the supplied text.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                    }
                ]
                if self.mode in SDK_MODES
                else []
            )
            response = _result(request_id, {"tools": tools})
            if self.mode == "unknown-response-id":
                return [_result(999, {}), response]
            if self.mode == "invalid-json-rpc-error":
                return [{"jsonrpc": "2.0", "id": request_id, "error": "broken"}]
            if self.pending_initialize is not None:
                initialize = _result(self.pending_initialize["id"], INITIALIZE_RESULT)
                self.pending_initialize = None
                return [response, initialize]
            return [response]

        if method == "tools/call" and self.mode in SDK_MODES:
            params = message.get("params")
            arguments = params.get("arguments") if isinstance(params, dict) else None
            text = arguments.get("text") if isinstance(arguments, dict) else None
            if (
                not isinstance(params, dict)
                or params.get("name") != "echo"
                or not isinstance(text, str)
            ):
                return [
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": "invalid echo arguments"},
                    }
                ]
            if self.mode == "sdk-hang":
                return []
            return [
                _result(
                    request_id,
                    {
                        "content": [{"type": "text", "text": text}],
                        "isError": False,
                    },
                )
            ]

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
            if self.mode == "invalid-server-request-id":
                return [
                    {
                        "id": None,
                        "jsonrpc": "2.0",
                        "method": "ping",
                    },
                    _result(request_id, {}),
                ]
            if self.mode == "invalid-notification-params":
                return [
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/message",
                        "params": [],
                    },
                    _result(request_id, {}),
                ]
            if self.mode == "invalid-null-notification-params":
                return [
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/message",
                        "params": None,
                    },
                    _result(request_id, {}),
                ]
            if self.mode == "notification-before-response":
                return [
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/message",
                        "params": {"level": "info"},
                    },
                    _result(request_id, {}),
                ]
            if self.mode == "server-ping-before-response":
                return [
                    {
                        "id": "server-ping",
                        "jsonrpc": "2.0",
                        "method": "ping",
                    },
                    _result(request_id, {}),
                ]
            return [_result(request_id, {})]

        return [_result(request_id, {})] if "id" in message else []


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({**payload, "pid": os.getpid()}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _write_stdio_report(
    report: Path | None,
    state: PeerState,
    protocol_version: str,
    *,
    clean_exit: bool,
) -> None:
    if report is None:
        return
    payload = {
        "clean_exit": clean_exit,
        "initialize_protocol_versions": state.initialize_protocol_versions,
        "methods": state.stdio_methods,
        "negotiated_protocol_version": protocol_version,
    }
    if state.mode == "server-ping-before-response":
        payload["server_ping_responses"] = len(state.server_ping_responses)
    _write_json(report, payload)


def run_stdio(
    mode: str,
    *,
    protocol_version: str = "2025-11-25",
    report: Path | None = None,
) -> int:
    state = PeerState(mode, negotiated_protocol_version=protocol_version)
    clean_exit = False
    _write_stdio_report(report, state, protocol_version, clean_exit=False)
    try:
        for raw_line in sys.stdin:
            message = json.loads(raw_line)
            replies = state.handle_stdio(message)
            _write_stdio_report(report, state, protocol_version, clean_exit=False)
            for reply in replies:
                print(json.dumps(reply, separators=(",", ":")), flush=True)
        clean_exit = True
        return 0
    except Exception as exc:
        print(f"fixture input error: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        _write_stdio_report(
            report,
            state,
            protocol_version,
            clean_exit=clean_exit,
        )


class ControlledHTTPPeer(AbstractContextManager["ControlledHTTPPeer"]):
    def __init__(
        self,
        mode: str,
        protocol_version: str = "2025-11-25",
    ) -> None:
        self.state = PeerState(
            mode,
            negotiated_protocol_version=protocol_version,
        )
        state = self.state
        hang_release = threading.Event()
        self._hang_release = hang_release

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
                if state.mode in SDK_MODES and (
                    self.path != "/mcp"
                    or self.headers.get_content_type() != "application/json"
                ):
                    self._send(415)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                message = json.loads(self.rfile.read(length))
                state.post_methods.append(message.get("method"))
                state.post_accepts.append(self.headers.get("Accept"))
                state.post_session_ids.append(self.headers.get("MCP-Session-Id"))
                state.post_protocol_versions.append(
                    self.headers.get("MCP-Protocol-Version")
                )
                state.post_authorizations.append(self.headers.get("Authorization"))
                if state.mode in SDK_MODES:
                    replies = state.handle_stdio(message)
                    if "id" not in message:
                        self._send(202)
                        return
                    if not replies:
                        hang_release.wait(timeout=30)
                        return
                    if len(replies) != 1:
                        raise RuntimeError("SDK HTTP mode returned multiple replies")
                    body = json.dumps(replies[0]).encode()
                    headers = (
                        {"MCP-Session-Id": state.session_id}
                        if message.get("method") == "initialize"
                        else {}
                    )
                    self._send(200, body, headers=headers)
                    return
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
                if state.mode in SDK_MODES and self.path != "/mcp":
                    self._send(404)
                    return
                state.last_event_ids.append(self.headers.get("Last-Event-ID"))
                state.session_ids.append(self.headers.get("MCP-Session-Id"))
                state.protocol_versions.append(self.headers.get("MCP-Protocol-Version"))
                state.get_count += 1
                if state.mode in SDK_MODES:
                    self._send(405)
                    return
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
                if state.mode in SDK_MODES and self.path != "/mcp":
                    self._send(404)
                    return
                state.delete_count += 1
                state.delete_session_ids.append(self.headers.get("MCP-Session-Id"))
                state.delete_protocol_versions.append(
                    self.headers.get("MCP-Protocol-Version")
                )
                state.delete_authorizations.append(self.headers.get("Authorization"))
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
        self._hang_release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise RuntimeError("fixture HTTP thread did not stop")
        if self._server.socket.fileno() != -1:
            raise RuntimeError("fixture HTTP server socket did not close")
        try:
            probe = socket.create_connection((self.host, self.port), timeout=0.5)
        except OSError:
            return
        probe.close()
        raise RuntimeError("fixture HTTP listener still accepts connections")


def run_http(
    mode: str,
    *,
    protocol_version: str,
    ready: Path,
    report: Path,
    expected_authorization: str | None,
) -> int:
    peer = ControlledHTTPPeer(mode, protocol_version=protocol_version)
    clean_exit = False
    listener_closed = False
    try:
        with peer:
            _write_json(ready, {"url": peer.url})
            sys.stdin.read()
        listener_closed = True
        clean_exit = True
        return 0
    except Exception as exc:
        print(f"fixture HTTP error: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        state = peer.state
        _write_json(
            report,
            {
                "accept_consistent": state.post_accepts
                == ["application/json, text/event-stream"] * 4,
                "authorization_consistent": state.post_authorizations
                == [expected_authorization] * 4
                and state.delete_authorizations == [expected_authorization],
                "clean_exit": clean_exit,
                "delete_count": state.delete_count,
                "listener_closed": listener_closed,
                "methods": state.post_methods,
                "negotiated_protocol_version": protocol_version,
                "protocol_version_preserved": state.post_protocol_versions
                == [None, protocol_version, protocol_version, protocol_version]
                and state.delete_protocol_versions == [protocol_version],
                "session_preserved": state.post_session_ids
                == [None, state.session_id, state.session_id, state.session_id]
                and state.delete_session_ids == [state.session_id],
            },
        )


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
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument("--stdio", action="store_true")
    transport.add_argument("--http", action="store_true")
    parser.add_argument("--mode", required=True)
    parser.add_argument(
        "--protocol-version",
        choices=("2025-06-18", "2025-11-25"),
        default="2025-11-25",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--authorization-env")
    args = parser.parse_args()
    if args.stdio:
        if args.ready is not None or args.authorization_env is not None:
            parser.error("--ready and --authorization-env require --http")
        return run_stdio(
            args.mode,
            protocol_version=args.protocol_version,
            report=args.report,
        )
    if args.ready is None or args.report is None:
        parser.error("--http requires --ready and --report")
    expected_authorization = None
    if args.authorization_env is not None:
        expected_authorization = os.environ.get(args.authorization_env)
        if not expected_authorization:
            parser.error(
                "--authorization-env must name a non-empty environment variable"
            )
    return run_http(
        args.mode,
        protocol_version=args.protocol_version,
        ready=args.ready,
        report=args.report,
        expected_authorization=expected_authorization,
    )


if __name__ == "__main__":
    raise SystemExit(main())
