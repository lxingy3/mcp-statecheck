"""Deterministic subscription peer used only by controlled wire tests."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROTOCOL = "2026-07-28"
RESOURCE = "file:///controlled/config.json"
MODES = (
    "conforming",
    "notification-before-ack",
    "wrong-subscription-id",
    "unsolicited-notification",
    "held-cancel",
    "late-after-cancel",
    "drop-first",
)
SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"


def _error(request_id: object, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32602, "message": message},
    }


def _notice(method: str, request_id: int | str, **fields: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": {"_meta": {SUBSCRIPTION_ID: request_id}, **fields},
    }


def respond(message: object, mode: str) -> list[dict[str, Any]]:
    """Return a finite stream; no mode reads the independent oracle."""
    if mode not in MODES:
        raise ValueError("unknown controlled subscription mode")
    if not isinstance(message, dict) or type(message.get("id")) not in (int, str):
        return [_error(None, "invalid request")]
    request_id = message["id"]
    params = message.get("params")
    if message.get("jsonrpc") != "2.0" or not isinstance(params, dict):
        return [_error(request_id, "invalid JSON-RPC request")]
    meta = params.get("_meta")
    if (
        not isinstance(meta, dict)
        or meta.get("io.modelcontextprotocol/protocolVersion") != PROTOCOL
    ):
        return [_error(request_id, "missing modern protocol version")]
    if not isinstance(
        meta.get("io.modelcontextprotocol/clientInfo"), dict
    ) or not isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict):
        return [_error(request_id, "missing client metadata")]
    if message.get("method") == "tools/list":
        return [
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"resultType": "complete", "tools": []},
            }
        ]
    if message.get("method") != "subscriptions/listen":
        return [_error(request_id, "unsupported request")]
    requested = params.get("notifications")
    if (
        not isinstance(requested, dict)
        or set(requested) - {"toolsListChanged", "resourceSubscriptions"}
        or any(
            key == "toolsListChanged"
            and type(value) is not bool
            or key == "resourceSubscriptions"
            and (
                not isinstance(value, list)
                or any(not isinstance(uri, str) for uri in value)
            )
            for key, value in requested.items()
        )
    ):
        return [_error(request_id, "invalid notification filter")]

    agreed: dict[str, Any] = {}
    if requested.get("toolsListChanged") is True:
        agreed["toolsListChanged"] = True
    if RESOURCE in requested.get("resourceSubscriptions", []):
        agreed["resourceSubscriptions"] = [RESOURCE]
    ack = _notice(
        "notifications/subscriptions/acknowledged", request_id, notifications=agreed
    )
    delivered: list[dict[str, Any]] = []
    if agreed.get("toolsListChanged"):
        delivered.append(_notice("notifications/tools/list_changed", request_id))
    if agreed.get("resourceSubscriptions"):
        delivered.append(
            _notice("notifications/resources/updated", request_id, uri=RESOURCE)
        )
    if mode == "notification-before-ack" and delivered:
        ack, delivered[0] = delivered[0], ack
    elif mode == "wrong-subscription-id" and delivered:
        delivered[0]["params"]["_meta"][SUBSCRIPTION_ID] = 999
    elif mode == "unsolicited-notification":
        delivered.append(_notice("notifications/prompts/list_changed", request_id))
    finished = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"resultType": "complete", "_meta": {SUBSCRIPTION_ID: request_id}},
    }
    if mode in {"held-cancel", "late-after-cancel", "drop-first"} and request_id == 1:
        return [ack, *delivered]
    return [ack, *delivered, finished]


class ControlledSubscriptionHTTPPeer(
    AbstractContextManager["ControlledSubscriptionHTTPPeer"]
):
    """Serve finite SSE responses on loopback and verify listener cleanup."""

    def __init__(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("unknown controlled subscription mode")
        self.closed = False
        self.cancel_seen = threading.Event()
        self._stop = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                if (
                    self.path != "/mcp"
                    or self.headers.get_content_type() != "application/json"
                ):
                    self.send_error(400)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024 * 1024:
                        raise ValueError("invalid body length")
                    message = json.loads(self.rfile.read(length))
                except (ValueError, OSError):
                    self.send_error(400)
                    return
                if (
                    not isinstance(message, dict)
                    or self.headers.get("MCP-Protocol-Version") != PROTOCOL
                    or self.headers.get("Mcp-Method") != message.get("method")
                    or self.headers.get("Mcp-Name") is not None
                    or self.headers.get("MCP-Session-Id") is not None
                ):
                    self.send_error(400)
                    return
                messages = respond(message, mode)
                if message.get("method") == "subscriptions/listen":
                    body = b"".join(
                        b"data: "
                        + json.dumps(item, separators=(",", ":")).encode()
                        + b"\n\n"
                        for item in messages
                    )
                    content_type = "text/event-stream"
                else:
                    body = json.dumps(messages[0], separators=(",", ":")).encode()
                    content_type = "application/json"
                if mode == "held-cancel" and message.get("id") == 1:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                        self.wfile.flush()
                        while not owner._stop.wait(0.05):
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                    except OSError:
                        owner.cancel_seen.set()
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def do_GET(self) -> None:  # noqa: N802
                self.send_error(405)

            def do_DELETE(self) -> None:  # noqa: N802
                self.send_error(405)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}
        )
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/mcp"

    def __enter__(self) -> ControlledSubscriptionHTTPPeer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self.closed = not self._thread.is_alive() and self._server.socket.fileno() == -1
        if not self.closed:
            raise RuntimeError("controlled subscription HTTP peer did not close")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=MODES, default="conforming")
    mode = parser.parse_args().mode
    cancelled = False
    for line in sys.stdin:
        request = json.loads(line)
        if not isinstance(request, dict):
            messages = respond(request, mode)
        elif mode in {"held-cancel", "late-after-cancel"}:
            if request.get("method") == "notifications/cancelled":
                params = request.get("params")
                cancelled = isinstance(params, dict) and params.get("requestId") == 1
                if cancelled and mode == "late-after-cancel":
                    late = _notice("notifications/tools/list_changed", 1)
                    print(json.dumps(late, separators=(",", ":")), flush=True)
                continue
            if (
                request.get("method") == "subscriptions/listen"
                and request.get("id") == 2
                and not cancelled
            ):
                messages = [_error(2, "first subscription was not cancelled")]
            else:
                messages = respond(request, mode)
        else:
            messages = respond(request, mode)
        for message in messages:
            print(json.dumps(message, separators=(",", ":")), flush=True)
        if (
            mode == "drop-first"
            and isinstance(request, dict)
            and request.get("method") == "subscriptions/listen"
            and request.get("id") == 1
        ):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
