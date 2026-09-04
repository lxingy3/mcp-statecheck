"""Behavioral checks for the controlled Tasks extension peer."""

import base64
import json
import socket
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from mcp_statecheck._task_peer import ControlledTaskHTTPPeer, TaskPeerState


def request(method: str, **params: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            **params,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {
                    "name": "task-peer-tests",
                    "version": "0.1",
                },
                "io.modelcontextprotocol/clientCapabilities": {
                    "extensions": {"io.modelcontextprotocol/tasks": {}},
                    "elicitation": {"form": {}},
                },
            },
        },
    }


def create(state: TaskPeerState, scenario: str = "complete") -> dict[str, Any]:
    return state.handle(
        request("tools/call", name="task_fixture", arguments={"scenario": scenario})
    )["result"]


def test_completed_task_is_retrievable_and_terminal() -> None:
    state = TaskPeerState()
    created = create(state)
    assert created["resultType"] == "task"
    assert created["status"] == "working"
    assert created["taskId"] == "task-1"
    assert created["createdAt"] == "2026-09-04T00:00:00Z"
    assert created["ttlMs"] is None
    assert created["pollIntervalMs"] == 0
    completed = state.handle(request("tasks/get", taskId=created["taskId"]))
    assert completed["result"]["status"] == "completed"
    assert completed["result"]["resultType"] == "complete"
    assert completed["result"]["result"]["isError"] is False
    assert state.handle(request("tasks/get", taskId=created["taskId"])) == completed
    assert create(state)["taskId"] == "task-2"


def test_input_task_repeats_one_request_then_acknowledges_and_completes() -> None:
    state = TaskPeerState()
    task_id = create(state, "input")["taskId"]
    pending = state.handle(request("tasks/get", taskId=task_id))["result"]
    assert pending["status"] == "input_required"
    assert pending["inputRequests"]["approval"]["method"] == "elicitation/create"
    assert state.handle(request("tasks/get", taskId=task_id))["result"] == pending
    ack = state.handle(
        request(
            "tasks/update",
            taskId=task_id,
            inputResponses={
                "approval": {"action": "accept", "content": {"approved": True}}
            },
        )
    )
    assert ack["result"] == {"resultType": "complete"}
    assert (
        state.handle(request("tasks/get", taskId=task_id))["result"]["status"]
        == "working"
    )
    assert (
        state.handle(request("tasks/get", taskId=task_id))["result"]["status"]
        == "completed"
    )


def test_input_poll_requires_form_capability_on_that_request() -> None:
    state = TaskPeerState()
    task_id = create(state, "input")["taskId"]
    message = request("tasks/get", taskId=task_id)
    message["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"].pop(
        "elicitation", None
    )
    error = state.handle(message)["error"]
    assert error["code"] == -32021
    assert error["data"]["requiredCapabilities"] == {"elicitation": {"form": {}}}
    pending = state.handle(request("tasks/get", taskId=task_id))["result"]
    assert pending["status"] == "input_required"
    assert pending["inputRequests"]["approval"]["method"] == "elicitation/create"


def test_cancellation_acknowledges_before_status_changes() -> None:
    state = TaskPeerState()
    task_id = create(state, "cancel")["taskId"]
    assert (
        state.handle(request("tasks/get", taskId=task_id))["result"]["status"]
        == "working"
    )
    assert state.handle(request("tasks/cancel", taskId=task_id))["result"] == {
        "resultType": "complete"
    }
    assert (
        state.handle(request("tasks/get", taskId=task_id))["result"]["status"]
        == "working"
    )
    cancelled = state.handle(request("tasks/get", taskId=task_id))["result"]
    assert cancelled["status"] == "cancelled"
    assert "result" not in cancelled
    assert state.handle(request("tasks/get", taskId=task_id))["result"] == cancelled


@pytest.mark.parametrize(
    "scenario,status", [("fail", "failed"), ("tool_error", "completed")]
)
def test_task_errors_distinguish_protocol_and_tool_failure(
    scenario: str, status: str
) -> None:
    state = TaskPeerState()
    task_id = create(state, scenario)["taskId"]
    finished = state.handle(request("tasks/get", taskId=task_id))["result"]
    assert finished["status"] == status
    if scenario == "fail":
        assert finished["error"]["code"] == -32603
        assert "result" not in finished
    else:
        assert finished["result"]["isError"] is True
        assert "error" not in finished


def test_deliberate_defects_are_observable_at_the_protocol_boundary() -> None:
    state = TaskPeerState("task-terminal-regression")
    task_id = create(state)["taskId"]
    assert (
        state.handle(request("tasks/get", taskId=task_id))["result"]["status"]
        == "completed"
    )
    assert (
        state.handle(request("tasks/get", taskId=task_id))["result"]["status"]
        == "working"
    )
    state = TaskPeerState("task-result-shape")
    task_id = create(state)["taskId"]
    result = state.handle(request("tasks/get", taskId=task_id))["result"]
    assert result["status"] == "completed" and "result" not in result
    state = TaskPeerState("task-input-key-reuse")
    task_id = create(state, "input")["taskId"]
    first = state.handle(request("tasks/get", taskId=task_id))["result"]
    state.handle(
        request(
            "tasks/update",
            taskId=task_id,
            inputResponses={"approval": {"action": "accept"}},
        )
    )
    next_result = state.handle(request("tasks/get", taskId=task_id))["result"]
    assert (
        set(first["inputRequests"]) == set(next_result["inputRequests"]) == {"approval"}
    )
    assert first["inputRequests"] != next_result["inputRequests"]


def test_capability_is_checked_on_every_request() -> None:
    state = TaskPeerState()
    task_id = create(state)["taskId"]
    for method in ("tasks/get", "tasks/update", "tasks/cancel"):
        message = request(method, taskId=task_id, inputResponses={})
        message["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] = {}
        error = state.handle(message)["error"]
        assert error["code"] == -32021
        assert error["data"]["requiredCapabilities"]["extensions"] == {
            "io.modelcontextprotocol/tasks": {}
        }
    message = request(
        "tools/call", name="task_fixture", arguments={"scenario": "complete"}
    )
    message["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] = {}
    assert state.handle(message)["result"]["resultType"] == "complete"
    assert create(state)["taskId"] == "task-2"


@pytest.mark.parametrize("extension", [None, True, [], "enabled"])
def test_malformed_extension_is_rejected_without_creating_a_task(
    extension: Any,
) -> None:
    state = TaskPeerState()
    message = request(
        "tools/call", name="task_fixture", arguments={"scenario": "complete"}
    )
    message["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"][
        "extensions"
    ]["io.modelcontextprotocol/tasks"] = extension
    assert state.handle(message)["error"]["code"] == -32602
    assert create(state)["taskId"] == "task-1"


@pytest.mark.parametrize("method", ["tasks/get", "tasks/update", "tasks/cancel"])
def test_unknown_tasks_return_invalid_params(method: str) -> None:
    state = TaskPeerState()
    assert (
        state.handle(request(method, taskId="missing", inputResponses={}))["error"][
            "code"
        ]
        == -32602
    )


def test_discovery_and_tool_catalog_describe_the_fixture() -> None:
    state = TaskPeerState()
    discovery = state.handle(request("server/discover"))["result"]
    assert discovery["capabilities"]["extensions"] == {
        "io.modelcontextprotocol/tasks": {}
    }
    assert discovery["supportedVersions"] == ["2026-07-28"]
    assert (
        state.handle(request("tools/list"))["result"]["tools"][0]["name"]
        == "task_fixture"
    )
    assert state.handle(request("not/a/method"))["error"]["code"] == -32601


@pytest.mark.parametrize(
    "params",
    [
        {"name": "other", "arguments": {"scenario": "complete"}},
        {"name": "task_fixture", "arguments": {"scenario": "unknown"}},
        {"name": "task_fixture", "arguments": None},
    ],
)
def test_invalid_tool_calls_do_not_create_tasks(params: dict[str, Any]) -> None:
    state = TaskPeerState()
    assert state.handle(request("tools/call", **params))["error"]["code"] == -32602
    assert create(state)["taskId"] == "task-1"


def test_stdio_and_http_execute_the_same_task_lifecycle_and_close() -> None:
    messages = [
        request("tools/call", name="task_fixture", arguments={"scenario": "complete"}),
        request("tasks/get", taskId="task-1"),
        request("tasks/get", taskId="task-1"),
    ]
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "mcp_statecheck._task_peer",
            "--mode",
            "conforming",
        ],
        input="".join(json.dumps(message) + "\n" for message in messages),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=15,
        check=True,
    )
    stdio = [json.loads(line) for line in completed.stdout.splitlines()]
    assert not completed.stderr
    with ControlledTaskHTTPPeer() as peer, httpx.Client(timeout=5) as client:
        http = []
        for message in messages:
            response = client.post(
                peer.url,
                json=message,
                headers={
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": message["method"],
                    "Mcp-Name": "task_fixture"
                    if message["method"] == "tools/call"
                    else "task-1",
                },
            )
            assert response.status_code == 200
            http.append(response.json())
        assert not peer.closed
    assert http == stdio
    assert peer.closed
    with pytest.raises(OSError):
        socket.create_connection(
            ("127.0.0.1", int(peer.url.split(":")[2].split("/")[0])), timeout=0.5
        )


def test_returned_result_cannot_mutate_stored_task_state() -> None:
    state = TaskPeerState()
    task_id = create(state)["taskId"]
    first = state.handle(request("tasks/get", taskId=task_id))["result"]
    first["result"]["content"][0]["text"] = "changed outside the peer"
    second = state.handle(request("tasks/get", taskId=task_id))["result"]
    assert second["result"]["content"][0]["text"] == "controlled task result"


@pytest.mark.parametrize(
    "header,value",
    [
        ("MCP-Protocol-Version", "2025-11-25"),
        ("Mcp-Method", "tasks/cancel"),
        ("Mcp-Name", "different-task"),
        ("MCP-Session-Id", "obsolete-session"),
    ],
)
def test_http_rejects_incorrect_routing_headers(header: str, value: str) -> None:
    with ControlledTaskHTTPPeer() as peer, httpx.Client(timeout=5) as client:
        headers = {
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tasks/get",
            "Mcp-Name": "task-1",
        }
        headers[header] = value
        assert (
            client.post(
                peer.url, json=request("tasks/get", taskId="task-1"), headers=headers
            ).status_code
            == 400
        )
        assert client.get(peer.url).status_code == 405
        assert client.delete(peer.url).status_code == 405


@pytest.mark.parametrize("task_id", ["task-\u2603", "=?base64?dGFzay0x?=", " task-1 "])
def test_http_routing_encodes_unicode_and_literal_sentinels(task_id: str) -> None:
    encoded = base64.b64encode(task_id.encode()).decode("ascii")
    with ControlledTaskHTTPPeer() as peer, httpx.Client(timeout=5) as client:
        response = client.post(
            peer.url,
            json=request("tasks/get", taskId=task_id),
            headers={
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tasks/get",
                "Mcp-Name": f"=?base64?{encoded}?=",
            },
        )
        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32602


def test_http_exit_closes_a_partial_upload() -> None:
    connection = None
    try:
        with ControlledTaskHTTPPeer() as peer:
            url = urlsplit(peer.url)
            connection = socket.create_connection((url.hostname, url.port), timeout=5)
            connection.sendall(
                b"POST /mcp HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{"
            )
            # A completed second exchange ensures the listener has accepted work.
            with httpx.Client(timeout=5) as client:
                assert client.get(peer.url).status_code == 405
            # Keep the partial upload open while the peer exits.
        assert peer.closed
        try:
            remaining = connection.recv(4096)
        except ConnectionResetError:
            remaining = b""
        assert not remaining or b"400" in remaining
    finally:
        if connection is not None:
            connection.close()


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "1.0", "id": 1, "method": "tasks/get"},
        {"jsonrpc": "2.0", "id": True, "method": "tasks/get"},
        {"jsonrpc": "2.0", "method": "tasks/get"},
        {"jsonrpc": "2.0", "id": 1, "method": []},
    ],
)
def test_invalid_request_envelope_returns_jsonrpc_error(
    message: dict[str, Any],
) -> None:
    assert TaskPeerState().handle(message)["error"]["code"] == -32600


def test_unissued_and_repeated_input_responses_do_not_change_task_progress() -> None:
    state = TaskPeerState()
    task_id = create(state, "input")["taskId"]
    update = request(
        "tasks/update",
        taskId=task_id,
        inputResponses={"approval": {"action": "accept"}},
    )
    assert state.handle(update)["result"] == {"resultType": "complete"}
    assert (
        state.handle(request("tasks/get", taskId=task_id))["result"]["status"]
        == "input_required"
    )
    state.handle(
        request(
            "tasks/update",
            taskId=task_id,
            inputResponses={"not-issued": {"action": "accept"}},
        )
    )
    assert (
        state.handle(request("tasks/get", taskId=task_id))["result"]["status"]
        == "input_required"
    )
    state.handle(update)
    state.handle(update)
    assert (
        state.handle(request("tasks/get", taskId=task_id))["result"]["status"]
        == "working"
    )
    completed = state.handle(request("tasks/get", taskId=task_id))["result"]
    assert completed["status"] == "completed"
    state.handle(update)
    state.handle(request("tasks/cancel", taskId=task_id))
    assert state.handle(request("tasks/get", taskId=task_id))["result"] == completed
