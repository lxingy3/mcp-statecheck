from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

from mcp_statecheck.execution import (
    ExecutionProtocolError,
    execute_http,
    execute_stdio,
)
from mcp_statecheck.model import Action, ActionKind
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
