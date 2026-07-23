from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

from mcp_statecheck.execution import ExecutionResult
from mcp_statecheck.invariants import detect_failure
from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.replay import (
    ReplayInfrastructureError,
    replay_http_failure,
    replay_stdio_failure,
)

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


def test_http_failure_replays_with_ten_fresh_executions() -> None:
    actions = (
        Action("connect", ActionKind.CONNECT),
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
        ),
    )
    events = (
        {
            "fixture_source_kind": "http_error",
            "http_method": "POST",
            "kind": "timeout",
            "status": 503,
            "target_action_id": "initialize",
        },
    )
    failure = detect_failure(
        actions,
        events,
        fixture_id="http-error-as-timeout",
    )
    assert failure is not None
    calls = 0

    async def executor(
        candidate: tuple[Action, ...],
        timeout: float,
    ) -> ExecutionResult:
        nonlocal calls
        calls += 1
        assert candidate == actions
        assert timeout == 5
        return ExecutionResult(
            events,
            None,
            "",
            cleanup={"client_closed": True, "listener_closed": True},
        )

    async def scenario() -> None:
        result = await replay_http_failure(
            actions,
            executor,
            expected_signature=failure.signature,
            fixture_id="http-error-as-timeout",
            attempts=10,
            timeout=5,
        )

        assert calls == 10
        assert len(result.attempts) == 10
        assert len({id(attempt.execution) for attempt in result.attempts}) == 10
        assert all(attempt.execution.returncode is None for attempt in result.attempts)

    anyio.run(scenario)


def test_http_replay_requires_cleanup_confirmation() -> None:
    actions = (
        Action("connect", ActionKind.CONNECT),
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
        ),
    )

    async def executor(
        _: tuple[Action, ...],
        __: float,
    ) -> ExecutionResult:
        return ExecutionResult(
            (),
            None,
            "",
            cleanup={"client_closed": True, "listener_closed": False},
        )

    async def scenario() -> None:
        with pytest.raises(
            ReplayInfrastructureError,
            match=r"did not confirm listener_closed",
        ):
            await replay_http_failure(
                actions,
                executor,
                expected_signature="mcp-statecheck:v1:fixture",
                fixture_id="http-error-as-timeout",
                attempts=1,
                timeout=5,
            )

    anyio.run(scenario)


def test_http_replay_rejects_a_process_returncode() -> None:
    actions = (
        Action("connect", ActionKind.CONNECT),
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
        ),
    )

    async def executor(
        _: tuple[Action, ...],
        __: float,
    ) -> ExecutionResult:
        return ExecutionResult((), 0, "")

    async def scenario() -> None:
        with pytest.raises(
            ReplayInfrastructureError,
            match=r"expected None",
        ):
            await replay_http_failure(
                actions,
                executor,
                expected_signature="mcp-statecheck:v1:fixture",
                fixture_id="http-error-as-timeout",
                attempts=1,
                timeout=5,
            )

    anyio.run(scenario)
