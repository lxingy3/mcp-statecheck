"""One controlled MCP peer with selectable fault modes."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

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
    pending_initialize: dict[str, Any] | None = None
    duplicate_calls: list[dict[str, Any]] = field(default_factory=list)
    cancelled_call: dict[str, Any] | None = None
    late_response_sent: bool = False

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
            if not self.late_response_sent:
                self.cancelled_call = message
                return []
            return [_result(request_id, {"which": message["params"]["label"]})]

        if method == "notifications/cancelled" and self.cancelled_call is not None:
            late = _result(
                self.cancelled_call["id"],
                {"which": self.cancelled_call["params"]["label"]},
            )
            self.cancelled_call = None
            self.late_response_sent = True
            return [late]

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
                if state.mode == "http-error-as-timeout":
                    body = json.dumps({"error": "controlled outage"}).encode()
                    self._send(503, body)
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
