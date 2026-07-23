from __future__ import annotations

import sys
from pathlib import Path

from mcp_statecheck.execution import ExecutionResult
from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.stateful import (
    shrink_duplicate_request_id,
    shrink_http_error_as_timeout,
    shrink_late_response_correlation,
    shrink_request_before_initialize,
    shrink_second_sse_resume_token_loss,
)

PEER = Path(__file__).parent / "fixtures" / "peer.py"


async def _http_error_executor(
    actions: tuple[Action, ...],
    timeout: float,
) -> ExecutionResult:
    assert timeout > 0
    initialize = next(
        action for action in reversed(actions) if action.kind is ActionKind.INITIALIZE
    )
    return ExecutionResult(
        (
            {
                "fixture_source_kind": "http_error",
                "http_method": "POST",
                "kind": "timeout",
                "status": 503,
                "target_action_id": initialize.action_id,
            },
        ),
        None,
        "",
    )


async def _sse_resume_executor(
    actions: tuple[Action, ...],
    timeout: float,
) -> ExecutionResult:
    assert timeout > 0
    by_id = {action.action_id: action for action in actions}
    return ExecutionResult(
        (
            {
                "kind": "response",
                "outcome": "success",
                "target_action_id": "initialize",
            },
            {
                "kind": "sse_resume",
                "peer_last_event_id": None,
                "peer_protocol_version": "2025-11-25",
                "peer_session_id": "fixture-session",
                "received_event_id": "cursor-1",
                "sent_last_event_id": None,
                "session_id": "fixture-session",
                "stream_id": by_id["open-sse"].stream_id,
                "target_action_id": "open-sse",
            },
            {
                "kind": "sse_resume",
                "peer_last_event_id": "cursor-1",
                "peer_protocol_version": "2025-11-25",
                "peer_session_id": "fixture-session",
                "received_event_id": "cursor-2",
                "sent_last_event_id": "cursor-1",
                "session_id": "fixture-session",
                "stream_id": by_id["resume-1"].stream_id,
                "target_action_id": "resume-1",
            },
            {
                "kind": "sse_resume",
                "peer_last_event_id": None,
                "peer_protocol_version": "2025-11-25",
                "peer_session_id": "fixture-session",
                "received_event_id": "cursor-3",
                "sent_last_event_id": None,
                "session_id": "fixture-session",
                "stream_id": by_id["resume-2"].stream_id,
                "target_action_id": "resume-2",
            },
        ),
        None,
        "",
    )


def test_real_stdio_failure_shrinks_to_initialize_then_tools_list() -> None:
    result = shrink_request_before_initialize(
        (
            sys.executable,
            str(PEER),
            "--stdio",
            "--mode",
            "request-before-initialized",
        ),
        seed=20_260_716,
        timeout=5,
    )

    assert tuple((action.kind, action.method) for action in result.actions) == (
        (ActionKind.INITIALIZE, None),
        (ActionKind.REQUEST, "tools/list"),
    )
    assert result.failure.trigger_action_id == result.actions[-1].action_id
    assert result.execution.returncode == 0
    assert result.execution.stderr == ""
    assert tuple(event["target_action_id"] for event in result.execution.events) == (
        result.actions[-1].action_id,
        result.actions[0].action_id,
    )


def test_duplicate_request_id_shrinks_to_two_overlapping_calls() -> None:
    result = shrink_duplicate_request_id(
        (
            sys.executable,
            str(PEER),
            "--stdio",
            "--mode",
            "duplicate-concurrent-request-id",
        ),
        seed=20_260_717,
        timeout=5,
    )

    assert tuple((action.kind, action.method) for action in result.actions) == (
        (ActionKind.INITIALIZE, None),
        (ActionKind.RESPONSE, None),
        (ActionKind.INITIALIZED, None),
        (ActionKind.REQUEST, "tools/call"),
        (ActionKind.REQUEST, "tools/call"),
    )
    first_call, second_call = result.actions[-2:]
    assert first_call.mcp_request_id == second_call.mcp_request_id
    assert result.failure.kind == "messages.request_id_reused_within_session"
    assert result.failure.trigger_action_id == second_call.action_id
    assert result.execution.returncode == 0
    assert result.execution.stderr == ""
    assert tuple(event["target_action_id"] for event in result.execution.events) == (
        result.actions[0].action_id,
        None,
        None,
    )


def test_late_response_correlation_shrinks_to_one_cancelled_pair() -> None:
    result = shrink_late_response_correlation(
        (
            sys.executable,
            str(PEER),
            "--stdio",
            "--mode",
            "late-response-after-cancellation",
        ),
        seed=20_260_720,
        timeout=5,
    )

    assert tuple((action.kind, action.action_id) for action in result.actions) == (
        (ActionKind.INITIALIZE, "initialize"),
        (ActionKind.RESPONSE, "initialize-response"),
        (ActionKind.INITIALIZED, "initialized"),
        (ActionKind.REQUEST, "call-a"),
        (ActionKind.CANCEL, "cancel-a"),
        (ActionKind.REQUEST, "call-b"),
        (ActionKind.RESPONSE, "misattributed-late-response"),
        (ActionKind.RESPONSE, "misattributed-current-response"),
    )
    assert result.failure.kind == ("differential.response_correlated_to_wrong_request")
    assert result.failure.trigger_action_id == "call-b"
    assert result.execution.returncode == 0
    assert result.execution.stderr == ""
    assert tuple(event["target_action_id"] for event in result.execution.events) == (
        "initialize",
        "call-b",
        "call-a",
    )


def test_http_error_as_timeout_shrinks_to_connect_then_initialize() -> None:
    result = shrink_http_error_as_timeout(
        _http_error_executor,
        seed=20_260_723,
        timeout=5,
    )

    assert tuple((action.kind, action.action_id) for action in result.actions) == (
        (ActionKind.CONNECT, "connect"),
        (ActionKind.INITIALIZE, "initialize"),
    )
    assert result.failure.kind == "differential.http_status_reported_as_timeout"
    assert result.failure.trigger_action_id == "initialize"
    assert result.execution.returncode is None
    assert result.execution.stderr == ""


def test_second_sse_resume_loss_shrinks_to_two_expected_cursors() -> None:
    result = shrink_second_sse_resume_token_loss(
        _sse_resume_executor,
        seed=20_260_723,
        timeout=5,
    )

    assert tuple((action.kind, action.action_id) for action in result.actions) == (
        (ActionKind.INITIALIZE, "initialize"),
        (ActionKind.INITIALIZED, "initialized"),
        (ActionKind.OPEN_STREAM, "open-sse"),
        (ActionKind.RESUME_STREAM, "resume-1"),
        (ActionKind.RESUME_STREAM, "resume-2"),
    )
    assert tuple(action.resume_token for action in result.actions[-2:]) == (
        "cursor-1",
        "cursor-2",
    )
    assert result.failure.kind == "differential.sse_resume_token_lost"
    assert result.failure.trigger_action_id == "resume-2"
    assert result.execution.returncode is None
    assert result.execution.stderr == ""
