from __future__ import annotations

import sys
from pathlib import Path

import anyio

from mcp_statecheck.invariants import detect_failure
from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.replay import replay_stdio_failure

PEER = Path(__file__).parent / "fixtures" / "peer.py"


def test_minimized_failure_replays_ten_times_with_one_signature() -> None:
    actions = (
        Action(
            "initialize-pending",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
            capabilities={},
        ),
        Action(
            "tools-list-2",
            ActionKind.REQUEST,
            mcp_request_id=2,
            method="tools/list",
            payload={},
        ),
    )
    failure = detect_failure(actions, ())
    assert failure is not None

    async def scenario() -> None:
        result = await replay_stdio_failure(
            actions,
            (
                sys.executable,
                str(PEER),
                "--stdio",
                "--mode",
                "request-before-initialized",
            ),
            expected_signature=failure.signature,
            attempts=10,
            timeout=5,
        )

        assert result.expected_signature == failure.signature
        assert len(result.attempts) == 10
        assert {attempt.failure.signature for attempt in result.attempts} == {
            failure.signature
        }
        assert all(attempt.execution.returncode == 0 for attempt in result.attempts)
        assert all(attempt.execution.stderr == "" for attempt in result.attempts)
        first_events = result.attempts[0].execution.events
        assert all(
            attempt.execution.events == first_events for attempt in result.attempts
        )

    anyio.run(scenario)
