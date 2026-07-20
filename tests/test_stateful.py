from __future__ import annotations

import sys
from pathlib import Path

from mcp_statecheck.model import ActionKind
from mcp_statecheck.stateful import (
    shrink_duplicate_request_id,
    shrink_late_response_correlation,
    shrink_request_before_initialize,
)

PEER = Path(__file__).parent / "fixtures" / "peer.py"


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
