from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

from mcp_statecheck.execution import ExecutionProtocolError, execute_stdio
from mcp_statecheck.model import Action, ActionKind

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


def test_duplicate_mcp_request_ids_are_never_guessed() -> None:
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
