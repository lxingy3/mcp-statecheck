from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import httpx
import pytest

import mcp_statecheck.execution as execution_module
from mcp_statecheck.execution import (
    ExecutionProtocolError,
    execute_http,
    execute_stdio,
)
from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.transports.streamable_http import StreamableHTTPTransport
from tests.fixtures.peer import (
    ControlledHTTPPeer,
    execute_controlled_http_fault,
)

PEER = Path(__file__).parent / "fixtures" / "peer.py"


def test_canonical_actions_execute_over_real_stdio_with_explicit_targets() -> None:
    async def scenario() -> None:
        result = await execute_stdio(
            (
                Action(
                    "initialize-pending",
                    ActionKind.INITIALIZE,
                    mcp_request_id=1,
                    protocol_version="2025-11-25",
                    capabilities={},
                ),
                Action(
                    "tools-list",
                    ActionKind.REQUEST,
                    mcp_request_id=2,
                    method="tools/list",
                    payload={},
                ),
            ),
            (
                sys.executable,
                str(PEER),
                "--stdio",
                "--mode",
                "request-before-initialized",
            ),
            timeout=5,
        )

        assert result.returncode == 0
        assert result.stderr == ""
        assert result.events[0] == {
            "kind": "response",
            "mcp_request_id": 2,
            "outcome": "success",
            "payload": {"tools": []},
            "target_action_id": "tools-list",
        }
        assert result.events[1]["target_action_id"] == "initialize-pending"
        assert result.events[1]["outcome"] == "success"

    anyio.run(scenario)


def test_unknown_response_id_is_a_protocol_error() -> None:
    async def scenario() -> None:
        with pytest.raises(
            ExecutionProtocolError,
            match="does not match a pending request",
        ):
            await execute_stdio(
                (
                    Action(
                        "initialize",
                        ActionKind.INITIALIZE,
                        mcp_request_id=1,
                        protocol_version="2025-11-25",
                    ),
                    Action(
                        "tools-list",
                        ActionKind.REQUEST,
                        mcp_request_id=2,
                        method="tools/list",
                    ),
                ),
                (
                    sys.executable,
                    str(PEER),
                    "--stdio",
                    "--mode",
                    "unknown-response-id",
                ),
                timeout=5,
            )

    anyio.run(scenario)


def test_duplicate_internal_action_id_is_rejected_before_peer_start() -> None:
    async def scenario() -> None:
        with pytest.raises(
            ExecutionProtocolError,
            match="duplicate internal action_id",
        ):
            await execute_stdio(
                (
                    Action(
                        "same",
                        ActionKind.INITIALIZE,
                        mcp_request_id=1,
                        protocol_version="2025-11-25",
                    ),
                    Action(
                        "same",
                        ActionKind.REQUEST,
                        mcp_request_id=2,
                        method="tools/list",
                    ),
                ),
                ("mcp-statecheck-command-that-does-not-exist",),
                timeout=5,
            )

    anyio.run(scenario)


def test_json_rpc_error_requires_code_and_message() -> None:
    async def scenario() -> None:
        with pytest.raises(
            ExecutionProtocolError,
            match="invalid JSON-RPC error object",
        ):
            await execute_stdio(
                (
                    Action(
                        "initialize",
                        ActionKind.INITIALIZE,
                        mcp_request_id=1,
                        protocol_version="2025-11-25",
                    ),
                    Action(
                        "tools-list",
                        ActionKind.REQUEST,
                        mcp_request_id=2,
                        method="tools/list",
                    ),
                ),
                (
                    sys.executable,
                    str(PEER),
                    "--stdio",
                    "--mode",
                    "invalid-json-rpc-error",
                ),
                timeout=5,
            )

    anyio.run(scenario)


def test_response_barrier_initializes_before_duplicate_ids_are_never_guessed() -> None:
    async def scenario() -> None:
        result = await execute_stdio(
            (
                Action(
                    "initialize",
                    ActionKind.INITIALIZE,
                    mcp_request_id=1,
                    protocol_version="2025-11-25",
                ),
                Action(
                    "initialize-response",
                    ActionKind.RESPONSE,
                    target_action_id="initialize",
                    protocol_version="2025-11-25",
                    capabilities={"tools": {}},
                ),
                Action("initialized", ActionKind.INITIALIZED),
                Action(
                    "call-a",
                    ActionKind.REQUEST,
                    mcp_request_id=7,
                    method="tools/call",
                    payload={"label": "first"},
                ),
                Action(
                    "call-b",
                    ActionKind.REQUEST,
                    mcp_request_id=7,
                    method="tools/call",
                    payload={"label": "second"},
                ),
            ),
            (
                sys.executable,
                str(PEER),
                "--stdio",
                "--mode",
                "duplicate-concurrent-request-id",
            ),
            timeout=5,
        )

        assert result.events[0]["target_action_id"] == "initialize"
        duplicate_events = result.events[1:]
        assert [event["payload"] for event in duplicate_events] == [
            {"which": "second"},
            {"which": "first"},
        ]
        assert [event["target_action_id"] for event in duplicate_events] == [
            None,
            None,
        ]

    anyio.run(scenario)


def test_late_cancelled_result_can_be_correlated_to_the_wrong_request() -> None:
    async def scenario() -> None:
        result = await execute_stdio(
            (
                Action(
                    "initialize",
                    ActionKind.INITIALIZE,
                    mcp_request_id=1,
                    protocol_version="2025-11-25",
                ),
                Action(
                    "initialize-response",
                    ActionKind.RESPONSE,
                    target_action_id="initialize",
                ),
                Action("initialized", ActionKind.INITIALIZED),
                Action(
                    "call-a",
                    ActionKind.REQUEST,
                    mcp_request_id=21,
                    method="tools/call",
                    payload={
                        "arguments": {"fixtureCanary": "call-a"},
                        "name": "fixture",
                    },
                ),
                Action(
                    "cancel-a",
                    ActionKind.CANCEL,
                    mcp_request_id=21,
                    target_action_id="call-a",
                ),
                Action(
                    "call-b",
                    ActionKind.REQUEST,
                    mcp_request_id=22,
                    method="tools/call",
                    payload={
                        "arguments": {"fixtureCanary": "call-b"},
                        "name": "fixture",
                    },
                ),
                Action(
                    "misattributed-late-response",
                    ActionKind.RESPONSE,
                    target_action_id="call-b",
                ),
                Action(
                    "misattributed-current-response",
                    ActionKind.RESPONSE,
                    target_action_id="call-a",
                ),
            ),
            (
                sys.executable,
                str(PEER),
                "--stdio",
                "--mode",
                "late-response-after-cancellation",
            ),
            timeout=5,
        )

        assert result.returncode == 0
        assert result.stderr == ""
        assert [event["target_action_id"] for event in result.events] == [
            "initialize",
            "call-b",
            "call-a",
        ]
        assert [
            event["payload"].get("structuredContent", {}).get("fixtureCanary")
            for event in result.events[1:]
        ] == ["call-a", "call-b"]

    anyio.run(scenario)


def _http_actions() -> tuple[Action, ...]:
    return (
        Action("connect", ActionKind.CONNECT),
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
            capabilities={},
        ),
        Action("initialized", ActionKind.INITIALIZED),
        Action(
            "open-sse",
            ActionKind.OPEN_STREAM,
            stream_id="server-events",
        ),
        Action(
            "resume-1",
            ActionKind.RESUME_STREAM,
            stream_id="server-events",
            resume_token="cursor-1",
        ),
        Action(
            "resume-2",
            ActionKind.RESUME_STREAM,
            stream_id="server-events",
            resume_token="cursor-2",
        ),
    )


def _smoke_actions() -> tuple[Action, ...]:
    return (
        Action("connect", ActionKind.CONNECT),
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
            capabilities={},
        ),
        Action(
            "initialize-response",
            ActionKind.RESPONSE,
            target_action_id="initialize",
        ),
        Action("initialized", ActionKind.INITIALIZED),
        Action(
            "ping",
            ActionKind.REQUEST,
            mcp_request_id=2,
            method="ping",
            payload={},
        ),
        Action("ping-response", ActionKind.RESPONSE, target_action_id="ping"),
        Action(
            "tools-list",
            ActionKind.REQUEST,
            mcp_request_id=3,
            method="tools/list",
            payload={},
        ),
        Action(
            "tools-list-response",
            ActionKind.RESPONSE,
            target_action_id="tools-list",
        ),
    )


def test_stdio_execution_records_notifications_before_the_target_response() -> None:
    async def scenario() -> None:
        result = await execute_stdio(
            _smoke_actions()[1:6],
            (
                sys.executable,
                str(PEER),
                "--stdio",
                "--mode",
                "notification-before-response",
            ),
            timeout=5,
        )

        assert [event["kind"] for event in result.events] == [
            "response",
            "notification",
            "response",
        ]
        assert result.events[1]["method"] == "notifications/message"
        assert result.events[2]["target_action_id"] == "ping"

    anyio.run(scenario)


def test_stdio_execution_answers_a_server_ping(
    tmp_path: Path,
) -> None:
    report = tmp_path / "peer.json"

    async def scenario() -> None:
        result = await execute_stdio(
            _smoke_actions()[1:6],
            (
                sys.executable,
                str(PEER),
                "--stdio",
                "--mode",
                "server-ping-before-response",
                "--report",
                str(report),
            ),
            timeout=5,
        )

        assert [event["kind"] for event in result.events] == [
            "response",
            "server_request",
            "response",
        ]
        assert result.events[1]["method"] == "ping"
        assert result.events[1]["response_outcome"] == "success"

    anyio.run(scenario)
    assert json.loads(report.read_text())["server_ping_responses"] == 1


@pytest.mark.parametrize(
    ("mode", "message"),
    (
        ("invalid-server-request-id", "invalid ID"),
        ("invalid-notification-params", "invalid params"),
        ("invalid-null-notification-params", "invalid params"),
    ),
)
def test_stdio_execution_rejects_invalid_server_messages(
    mode: str,
    message: str,
) -> None:
    async def scenario() -> None:
        with pytest.raises(ExecutionProtocolError, match=message):
            await execute_stdio(
                _smoke_actions()[1:6],
                (
                    sys.executable,
                    str(PEER),
                    "--stdio",
                    "--mode",
                    mode,
                ),
                timeout=5,
            )

    anyio.run(scenario)


def test_stdio_execution_stops_after_initialize_error(
    tmp_path: Path,
) -> None:
    report = tmp_path / "peer.json"

    async def scenario() -> None:
        result = await execute_stdio(
            _smoke_actions()[1:],
            (
                sys.executable,
                str(PEER),
                "--stdio",
                "--mode",
                "initialize-error",
                "--report",
                str(report),
            ),
            timeout=5,
        )

        assert len(result.events) == 1
        assert result.events[0]["outcome"] == "error"
        assert result.events[0]["target_action_id"] == "initialize"

    anyio.run(scenario)
    assert json.loads(report.read_text())["methods"] == ["initialize"]


def test_stdio_execution_stops_after_invalid_initialize_result(
    tmp_path: Path,
) -> None:
    report = tmp_path / "peer.json"

    async def scenario() -> None:
        result = await execute_stdio(
            _smoke_actions()[1:],
            (
                sys.executable,
                str(PEER),
                "--stdio",
                "--mode",
                "initialize-invalid-result",
                "--report",
                str(report),
            ),
            timeout=5,
        )

        assert len(result.events) == 1
        assert result.events[0]["outcome"] == "success"
        assert result.events[0]["payload"] == {}
        assert result.events[0]["target_action_id"] == "initialize"

    anyio.run(scenario)
    assert json.loads(report.read_text())["methods"] == ["initialize"]


def test_http_execution_supports_requests_barriers_and_headers() -> None:
    async def scenario() -> None:
        with ControlledHTTPPeer("sdk-smoke") as peer:
            result = await execute_http(
                _smoke_actions(),
                peer.url,
                headers={"Authorization": "Bearer test-token"},
                timeout=5,
            )
            assert peer.state.post_methods == [
                "initialize",
                "notifications/initialized",
                "ping",
                "tools/list",
            ]
            assert peer.state.post_authorizations == ["Bearer test-token"] * 4

        assert [event["target_action_id"] for event in result.events] == [
            "initialize",
            "ping",
            "tools-list",
        ]
        assert result.events[-1]["payload"]["tools"][0]["name"] == "echo"
        assert result.cleanup == {"client_closed": True}

    anyio.run(scenario)


def test_http_execution_answers_server_request_before_sse_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        response_received = anyio.Event()
        requests: list[tuple[str, dict[str, object] | None]] = []

        class InitializeStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (
                    b'data: {"jsonrpc":"2.0","id":"server-ping","method":"ping"}\n\n'
                )
                with anyio.fail_after(1):
                    await response_received.wait()
                yield (
                    b'data: {"jsonrpc":"2.0","id":1,"result":'
                    b'{"protocolVersion":"2025-11-25","capabilities":{},'
                    b'"serverInfo":{"name":"fixture","version":"1"}}}\n\n'
                )

        async def handler(request: httpx.Request) -> httpx.Response:
            body = (
                json.loads(await request.aread()) if request.method == "POST" else None
            )
            requests.append((request.method, body))
            if body and body.get("method") == "initialize":
                return httpx.Response(
                    200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "MCP-Session-Id": "session-1",
                    },
                    stream=InitializeStream(),
                )
            if body and body.get("id") == "server-ping":
                assert body == {
                    "id": "server-ping",
                    "jsonrpc": "2.0",
                    "result": {},
                }
                assert request.headers["MCP-Session-Id"] == "session-1"
                assert request.headers["MCP-Protocol-Version"] == "2025-11-25"
                response_received.set()
                return httpx.Response(202)
            assert request.method == "DELETE"
            return httpx.Response(200)

        transport = StreamableHTTPTransport(
            "https://example.test/mcp",
            timeout=2,
            transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(
            execution_module,
            "StreamableHTTPTransport",
            lambda *_args, **_kwargs: transport,
        )

        result = await execution_module.execute_http(
            _smoke_actions()[:3],
            "https://example.test/mcp",
            timeout=2,
        )

        assert [event["kind"] for event in result.events] == [
            "server_request",
            "response",
        ]
        assert [method for method, _ in requests] == ["POST", "POST", "DELETE"]

    anyio.run(scenario)


def test_http_execution_answers_server_request_on_get_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        response_received = anyio.Event()
        request_methods: list[str] = []

        class ServerRequestStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (
                    b'data: {"jsonrpc":"2.0","id":"server-ping","method":"ping"}\n\n'
                )
                with anyio.fail_after(1):
                    await response_received.wait()

        async def handler(request: httpx.Request) -> httpx.Response:
            request_methods.append(request.method)
            if request.method == "GET":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    stream=ServerRequestStream(),
                )
            body = json.loads(await request.aread())
            assert body == {
                "id": "server-ping",
                "jsonrpc": "2.0",
                "result": {},
            }
            assert request.headers["MCP-Protocol-Version"] == "2025-11-25"
            response_received.set()
            return httpx.Response(202)

        transport = StreamableHTTPTransport(
            "https://example.test/mcp",
            timeout=2,
            protocol_version="2025-11-25",
            transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(
            execution_module,
            "StreamableHTTPTransport",
            lambda *_args, **_kwargs: transport,
        )

        result = await execution_module.execute_http(
            (Action("open", ActionKind.OPEN_STREAM, stream_id="events"),),
            "https://example.test/mcp",
            timeout=2,
        )

        assert result.events[0]["payload"]["method"] == "ping"
        assert request_methods == ["GET", "POST"]

    anyio.run(scenario)


def test_http_execution_uses_canonical_resume_tokens() -> None:
    async def scenario() -> None:
        with ControlledHTTPPeer("second-sse-resume-token-loss") as peer:
            result = await execute_http(_http_actions(), peer.url, timeout=5)
            assert peer.state.last_event_ids == [None, "cursor-1", "cursor-2"]
            assert peer.state.session_ids == [peer.state.session_id] * 3
            assert peer.state.protocol_versions == ["2025-11-25"] * 3
            assert peer.state.delete_count == 1
            assert peer.state.delete_session_ids == [peer.state.session_id]
            assert peer.state.delete_protocol_versions == ["2025-11-25"]

        sse_events = [event for event in result.events if event["kind"] == "sse_resume"]
        assert [event["sent_last_event_id"] for event in sse_events] == [
            None,
            "cursor-1",
            "cursor-2",
        ]
        assert [event["received_event_id"] for event in sse_events] == [
            "cursor-1",
            "cursor-2",
            "cursor-3",
        ]
        assert result.returncode is None
        assert result.stderr == ""
        assert result.cleanup == {"client_closed": True}

    anyio.run(scenario)


def test_http_execution_stops_after_initialize_error() -> None:
    async def scenario() -> None:
        with ControlledHTTPPeer("initialize-error") as peer:
            result = await execute_http(_http_actions()[:3], peer.url, timeout=5)
            assert peer.state.post_methods == ["initialize"]

        assert len(result.events) == 1
        assert result.events[0]["outcome"] == "error"
        assert result.events[0]["target_action_id"] == "initialize"
        assert result.cleanup == {"client_closed": True}

    anyio.run(scenario)


def test_controlled_http_error_preserves_the_wire_status() -> None:
    async def scenario() -> None:
        with ControlledHTTPPeer("http-error-as-timeout") as peer:
            wire_result = await execute_http(_http_actions()[:2], peer.url, timeout=5)
        assert wire_result.events == (
            {
                "http_method": "POST",
                "kind": "http_error",
                "status": 503,
                "target_action_id": "initialize",
            },
        )

        result = await execute_controlled_http_fault(
            _http_actions()[:2],
            "http-error-as-timeout",
            timeout=5,
        )

        assert result.events == (
            {
                "fixture_source_kind": "http_error",
                "http_method": "POST",
                "kind": "timeout",
                "status": 503,
                "target_action_id": "initialize",
            },
        )
        assert result.cleanup == {
            "client_closed": True,
            "listener_closed": True,
        }

    anyio.run(scenario)


def test_controlled_second_resume_drops_only_the_wire_token() -> None:
    actions = _http_actions()

    async def scenario() -> None:
        result = await execute_controlled_http_fault(
            actions,
            "second-sse-resume-token-loss",
            timeout=5,
        )

        sse_events = [event for event in result.events if event["kind"] == "sse_resume"]
        assert [event["sent_last_event_id"] for event in sse_events] == [
            None,
            "cursor-1",
            None,
        ]
        assert [event["peer_last_event_id"] for event in sse_events] == [
            None,
            "cursor-1",
            None,
        ]
        assert [event["received_event_id"] for event in sse_events] == [
            "cursor-1",
            "cursor-2",
            "cursor-3",
        ]
        assert [event["peer_protocol_version"] for event in sse_events] == [
            "2025-11-25"
        ] * 3
        assert len({event["peer_session_id"] for event in sse_events}) == 1
        assert actions[-1].resume_token == "cursor-2"
        assert len({event["session_id"] for event in sse_events}) == 1
        assert result.cleanup == {
            "client_closed": True,
            "listener_closed": True,
            "session_deleted": True,
        }

    anyio.run(scenario)
