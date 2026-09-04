"""Controlled, in-memory Tasks peer for repeatable protocol tests.

Task IDs and timestamps are deliberately deterministic fixture values. This peer
has no persistence or authentication and must not be deployed as a task service.
Progress is driven by requests, never wall-clock timers.
"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import sys
import threading
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
FIXED_TIMESTAMP = "2026-09-04T00:00:00Z"
MODES = (
    "conforming",
    "task-terminal-regression",
    "task-input-key-reuse",
    "task-result-shape",
)
SCENARIOS = ("complete", "input", "cancel", "fail", "tool_error")


def _result(request_id: object, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": deepcopy(payload)}


def _error(request_id: object, code: int, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message, **extra},
    }


def _capability(params: dict[str, Any]) -> bool:
    meta = params.get("_meta")
    if (
        not isinstance(meta, dict)
        or meta.get("io.modelcontextprotocol/protocolVersion") != PROTOCOL_VERSION
    ):
        raise ValueError("missing or invalid modern protocol metadata")
    info = meta.get("io.modelcontextprotocol/clientInfo")
    capabilities = meta.get("io.modelcontextprotocol/clientCapabilities")
    if not isinstance(info, dict) or not all(
        isinstance(info.get(key), str) and info[key] for key in ("name", "version")
    ):
        raise ValueError("missing or invalid client info")
    if not isinstance(capabilities, dict):
        raise ValueError("missing or invalid client capabilities")
    extensions = capabilities.get("extensions", {})
    if not isinstance(extensions, dict) or any(
        not isinstance(value, dict) for value in extensions.values()
    ):
        raise ValueError("extension capabilities must be objects")
    return TASKS_EXTENSION in extensions


def _tool_result(*, is_error: bool = False) -> dict[str, Any]:
    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": "controlled task result"}],
        "isError": is_error,
    }


def _missing_form_capability(
    request_id: object, params: dict[str, Any]
) -> dict[str, Any] | None:
    capabilities = params["_meta"]["io.modelcontextprotocol/clientCapabilities"]
    elicitation = capabilities.get("elicitation")
    if isinstance(elicitation, dict) and isinstance(elicitation.get("form"), dict):
        return None
    return _error(
        request_id,
        -32021,
        "Missing required client capability",
        data={"requiredCapabilities": {"elicitation": {"form": {}}}},
    )


def _task_result(task_id: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "resultType": "complete",
        "taskId": task_id,
        "status": status,
        "createdAt": FIXED_TIMESTAMP,
        "lastUpdatedAt": FIXED_TIMESTAMP,
        "ttlMs": None,
        "pollIntervalMs": 0,
        **extra,
    }


def _input_request(*, changed: bool = False) -> dict[str, Any]:
    return {
        "approval": {
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": "Approve the second operation."
                if changed
                else "Approve the operation.",
                "requestedSchema": {
                    "type": "object",
                    "properties": {"approved": {"type": "boolean"}},
                    "required": ["approved"],
                },
            },
        }
    }


@dataclass
class _Task:
    scenario: str
    input_answered: bool = False
    input_outstanding: bool = False
    polls_after_update: int = 0
    cancel_requested: bool = False
    polls_after_cancel: int = 0
    terminal: dict[str, Any] | None = None


@dataclass
class TaskPeerState:
    """A deterministic fixture state machine, independent of the task oracle."""

    mode: str = "conforming"
    _tasks: dict[str, _Task] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown task peer mode: {self.mode}")

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(message, dict):
            return _error(None, -32600, "expected one JSON-RPC request")
        request_id = message.get("id")
        valid_id = type(request_id) in (str, int)
        if (
            message.get("jsonrpc") != "2.0"
            or not valid_id
            or not isinstance(message.get("method"), str)
            or not message["method"]
        ):
            return _error(
                request_id if valid_id else None, -32600, "invalid JSON-RPC request"
            )
        params = message.get("params")
        if not isinstance(params, dict):
            return _error(request_id, -32602, "request params must be an object")
        try:
            supports_tasks = _capability(params)
        except ValueError as exc:
            return _error(request_id, -32602, str(exc))
        if message["method"] == "server/discover":
            return _result(
                request_id,
                {
                    "resultType": "complete",
                    "cacheScope": "private",
                    "ttlMs": 0,
                    "_meta": {
                        "io.modelcontextprotocol/serverInfo": {
                            "name": "controlled-task-peer",
                            "version": "0.1",
                        }
                    },
                    "capabilities": {"tools": {}, "extensions": {TASKS_EXTENSION: {}}},
                    "supportedVersions": [PROTOCOL_VERSION],
                },
            )
        if message["method"] == "tools/list":
            return _result(
                request_id,
                {
                    "resultType": "complete",
                    "cacheScope": "private",
                    "ttlMs": 0,
                    "tools": [
                        {
                            "name": "task_fixture",
                            "description": "Run a controlled task lifecycle.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "scenario": {
                                        "type": "string",
                                        "enum": list(SCENARIOS),
                                    }
                                },
                                "required": ["scenario"],
                                "additionalProperties": False,
                            },
                        }
                    ],
                },
            )
        if (
            message["method"] in {"tasks/get", "tasks/update", "tasks/cancel"}
            and not supports_tasks
        ):
            return _error(
                request_id,
                -32021,
                "Missing required client capability",
                data={"requiredCapabilities": {"extensions": {TASKS_EXTENSION: {}}}},
            )
        if message["method"] == "tools/call":
            arguments = params.get("arguments")
            if (
                params.get("name") != "task_fixture"
                or not isinstance(arguments, dict)
                or arguments.get("scenario") not in SCENARIOS
            ):
                return _error(request_id, -32602, "invalid task_fixture arguments")
            if not supports_tasks:
                return _result(
                    request_id,
                    _tool_result(is_error=arguments["scenario"] == "tool_error"),
                )
            task_id = f"task-{len(self._tasks) + 1}"
            self._tasks[task_id] = _Task(message["params"]["arguments"]["scenario"])
            return _result(
                request_id, _task_result(task_id, "working", resultType="task")
            )
        if message["method"] not in {"tasks/get", "tasks/update", "tasks/cancel"}:
            return _error(
                request_id, -32601, "method not supported by controlled task peer"
            )
        task_id = params.get("taskId")
        if not isinstance(task_id, str) or task_id not in self._tasks:
            return _error(request_id, -32602, "unknown taskId")
        task = self._tasks[task_id]
        if message["method"] == "tasks/cancel":
            task.cancel_requested = True
            return _result(request_id, {"resultType": "complete"})
        if message["method"] == "tasks/update":
            responses = params.get("inputResponses")
            if not isinstance(responses, dict) or any(
                not isinstance(value, dict) for value in responses.values()
            ):
                return _error(
                    request_id, -32602, "inputResponses must map keys to responses"
                )
            if (
                task.input_outstanding
                and "approval" in message["params"]["inputResponses"]
            ):
                task.input_answered = True
                task.input_outstanding = False
            return _result(request_id, {"resultType": "complete"})
        if task.terminal is not None:
            if self.mode == "task-terminal-regression":
                return _result(request_id, _task_result(task_id, "working"))
            return _result(request_id, dict(task.terminal))
        if task.cancel_requested:
            task.polls_after_cancel += 1
            if task.polls_after_cancel == 1:
                return _result(request_id, _task_result(task_id, "working"))
            task.terminal = _task_result(task_id, "cancelled")
            return _result(request_id, dict(task.terminal))
        if task.scenario == "cancel":
            return _result(request_id, _task_result(task_id, "working"))
        if task.scenario == "fail":
            task.terminal = _task_result(
                task_id,
                "failed",
                error={"code": -32603, "message": "controlled execution failure"},
            )
            return _result(request_id, dict(task.terminal))
        if task.scenario == "input":
            if not task.input_answered:
                error = _missing_form_capability(request_id, params)
                if error is not None:
                    return error
                task.input_outstanding = True
                return _result(
                    request_id,
                    _task_result(
                        task_id, "input_required", inputRequests=_input_request()
                    ),
                )
            task.polls_after_update += 1
            if self.mode == "task-input-key-reuse":
                error = _missing_form_capability(request_id, params)
                if error is not None:
                    return error
                return _result(
                    request_id,
                    _task_result(
                        task_id,
                        "input_required",
                        inputRequests=_input_request(changed=True),
                    ),
                )
            if task.polls_after_update == 1:
                return _result(request_id, _task_result(task_id, "working"))
        task.terminal = _task_result(
            task_id,
            "completed",
            result=_tool_result(is_error=task.scenario == "tool_error"),
        )
        if self.mode == "task-result-shape":
            task.terminal.pop("result")
        return _result(request_id, dict(task.terminal))


def _header_value(value: str) -> str:
    sentinel = value.startswith("=?base64?") and value.endswith("?=")
    if (
        not sentinel
        and value == value.strip(" \t")
        and all(
            character == "\t" or 0x20 <= ord(character) <= 0x7E for character in value
        )
    ):
        return value
    return f"=?base64?{base64.b64encode(value.encode('utf-8')).decode('ascii')}?="


class ControlledTaskHTTPPeer(AbstractContextManager["ControlledTaskHTTPPeer"]):
    """Loopback-only HTTP wrapper; context exit owns all sockets and threads."""

    def __init__(self, mode: str = "conforming") -> None:
        self.state = TaskPeerState(mode)
        self.closed = False
        state = self.state
        state_lock = threading.Lock()
        socket_lock = threading.Lock()
        connections: set[socket.socket] = set()
        self._connections = connections
        self._socket_lock = socket_lock

        class OwnedServer(ThreadingHTTPServer):
            def process_request(
                self, request: socket.socket, client_address: Any
            ) -> None:
                # Register before starting a handler so shutdown cannot miss a
                # connection whose worker has not reached setup yet.
                with socket_lock:
                    connections.add(request)
                super().process_request(request, client_address)

            def shutdown_request(self, request: socket.socket) -> None:
                try:
                    super().shutdown_request(request)
                finally:
                    with socket_lock:
                        connections.discard(request)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_: object) -> None:
                return

            def _send(self, status: int, payload: dict[str, Any] | None = None) -> None:
                body = (
                    b""
                    if payload is None
                    else json.dumps(payload, separators=(",", ":")).encode()
                )
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if body:
                        self.wfile.write(body)
                except OSError:
                    pass

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/mcp":
                    self._send(404)
                    return
                if self.headers.get_content_type() != "application/json":
                    self._send(415)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > 1024 * 1024:
                        self._send(400)
                        return
                    message = json.loads(self.rfile.read(length))
                except (ValueError, OSError):
                    self._send(400)
                    return
                if not isinstance(message, dict):
                    self._send(400)
                    return
                method = message.get("method")
                params = message.get("params", {})
                if not isinstance(method, str) or not isinstance(params, dict):
                    self._send(400)
                    return
                name = (
                    params.get("taskId")
                    if method in {"tasks/get", "tasks/update", "tasks/cancel"}
                    else params.get("name")
                    if method == "tools/call"
                    else None
                )
                expected_name = _header_value(name) if isinstance(name, str) else None
                if (
                    self.headers.get("MCP-Protocol-Version") != PROTOCOL_VERSION
                    or self.headers.get("Mcp-Method") != _header_value(method)
                    or self.headers.get("Mcp-Name") != expected_name
                    or self.headers.get("MCP-Session-Id") is not None
                ):
                    self._send(400)
                    return
                with state_lock:
                    response = state.handle(message)
                self._send(200, response)

            def do_GET(self) -> None:  # noqa: N802
                self._send(405)

            def do_DELETE(self) -> None:  # noqa: N802
                self._send(405)

        self._server = OwnedServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.05),
            name="controlled-task-http",
        )
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/mcp"

    def __enter__(self) -> ControlledTaskHTTPPeer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        if self.closed:
            return
        self._server.shutdown()
        with self._socket_lock:
            for connection in tuple(self._connections):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        self._server.server_close()
        self._thread.join(timeout=5)
        self.closed = not self._thread.is_alive() and self._server.socket.fileno() == -1
        if not self.closed:
            raise RuntimeError("controlled task HTTP peer did not close")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the controlled Tasks fixture over stdio."
    )
    parser.add_argument("--mode", choices=MODES, default="conforming")
    args = parser.parse_args()
    state = TaskPeerState(args.mode)
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = state.handle(message)
        except (ValueError, TypeError, KeyError):
            response = _error(None, -32700, "invalid controlled peer input")
        print(json.dumps(response, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
