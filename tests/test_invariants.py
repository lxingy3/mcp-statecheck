from dataclasses import replace

import pytest

from mcp_statecheck.invariants import LATE_RESPONSE_FIXTURE_ID, detect_failure
from mcp_statecheck.model import Action, ActionKind


def _initialized_session(
    prefix: str = "session", request_id: int = 1
) -> tuple[Action, ...]:
    initialize_action_id = f"{prefix}-initialize"
    return (
        Action(
            initialize_action_id,
            ActionKind.INITIALIZE,
            mcp_request_id=request_id,
            protocol_version="2025-11-25",
        ),
        Action(
            f"{prefix}-initialize-response",
            ActionKind.RESPONSE,
            target_action_id=initialize_action_id,
            protocol_version="2025-11-25",
            capabilities={},
        ),
        Action(f"{prefix}-initialized", ActionKind.INITIALIZED),
    )


def _late_cancellation_actions() -> tuple[Action, ...]:
    return (
        *_initialized_session(),
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
    )


def _late_correlation_events() -> tuple[dict[str, object], ...]:
    return (
        {
            "kind": "response",
            "mcp_request_id": 1,
            "payload": {},
            "target_action_id": "session-initialize",
        },
        {
            "kind": "response",
            "mcp_request_id": 22,
            "payload": {
                "content": [],
                "structuredContent": {"fixtureCanary": "call-a"},
            },
            "target_action_id": "call-b",
        },
        {
            "kind": "response",
            "mcp_request_id": 21,
            "payload": {
                "content": [],
                "structuredContent": {"fixtureCanary": "call-b"},
            },
            "target_action_id": "call-a",
        },
    )


def test_request_sent_before_initialize_response_has_stable_failure() -> None:
    actions = (
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
    )
    failure = detect_failure(actions, ())

    assert failure is not None
    assert failure.kind == "lifecycle.client_request_before_initialize_response"
    assert failure.trigger_action_id == "tools-list"
    assert failure.evidence == {
        "direction": "client_to_server",
        "lifecycle_boundary": "awaiting_initialize_response",
        "method": "tools/list",
        "subject": "client",
    }
    assert failure.signature.startswith("mcp-statecheck:v1:")
    assert len(failure.signature.removeprefix("mcp-statecheck:v1:")) == 64
    assert failure.signature == (
        "mcp-statecheck:v1:"
        "62237ccbaf578ac57aedd89b1b34c81c01913f6541dc3d3d414fbc180fd25dc2"
    )


def test_signature_ignores_internal_and_wire_request_ids() -> None:
    def failure_for(prefix: str, initialize_id: int, request_id: int) -> str:
        initialize_action_id = f"{prefix}-initialize"
        request_action_id = f"{prefix}-list"
        failure = detect_failure(
            (
                Action(
                    initialize_action_id,
                    ActionKind.INITIALIZE,
                    mcp_request_id=initialize_id,
                ),
                Action(
                    request_action_id,
                    ActionKind.REQUEST,
                    mcp_request_id=request_id,
                    method="tools/list",
                ),
            ),
            (),
        )
        assert failure is not None
        return failure.signature

    assert failure_for("one", 1, 2) == failure_for("other", 100, 900)


def test_pending_initialize_does_not_leak_across_disconnect() -> None:
    failure = detect_failure(
        (
            Action("initialize", ActionKind.INITIALIZE, mcp_request_id=1),
            Action("disconnect", ActionKind.DISCONNECT),
            Action(
                "request-on-next-connection",
                ActionKind.REQUEST,
                mcp_request_id=2,
                method="tools/list",
            ),
        ),
        (),
    )

    assert failure is None


def test_cancelled_initialize_still_awaits_a_server_response() -> None:
    failure = detect_failure(
        (
            Action("initialize", ActionKind.INITIALIZE, mcp_request_id=1),
            Action(
                "cancel-initialize",
                ActionKind.CANCEL,
                target_action_id="initialize",
            ),
            Action(
                "tools-list",
                ActionKind.REQUEST,
                mcp_request_id=2,
                method="tools/list",
            ),
        ),
        (),
    )

    assert failure is not None
    assert failure.trigger_action_id == "tools-list"


def test_ping_is_allowed_while_initialize_is_pending() -> None:
    failure = detect_failure(
        (
            Action("initialize", ActionKind.INITIALIZE, mcp_request_id=1),
            Action(
                "ping",
                ActionKind.REQUEST,
                mcp_request_id=2,
                method="ping",
            ),
        ),
        (),
    )

    assert failure is None


def test_request_after_initialize_response_is_not_an_early_request() -> None:
    failure = detect_failure(
        (
            Action("initialize", ActionKind.INITIALIZE, mcp_request_id=1),
            Action(
                "initialize-response",
                ActionKind.RESPONSE,
                target_action_id="initialize",
                protocol_version="2025-11-25",
                capabilities={},
            ),
            Action(
                "tools-list",
                ActionKind.REQUEST,
                mcp_request_id=2,
                method="tools/list",
            ),
        ),
        (),
    )

    assert failure is None


def test_reused_request_id_is_a_stable_session_failure() -> None:
    actions = (
        *_initialized_session(),
        Action(
            "call-a",
            ActionKind.REQUEST,
            mcp_request_id=7,
            method="tools/call",
        ),
        Action(
            "call-b",
            ActionKind.REQUEST,
            mcp_request_id=7,
            method="tools/call",
        ),
    )

    failure = detect_failure(actions, ())

    assert failure is not None
    assert failure.kind == "messages.request_id_reused_within_session"
    assert failure.trigger_action_id == "call-b"
    assert failure.evidence == {
        "direction": "client_to_server",
        "mcp_request_id": 7,
        "method": "tools/call",
        "overlap": "pending",
        "previous_action_id": "call-a",
        "previous_method": "tools/call",
        "session_scope": "same_session",
        "subject": "client",
    }
    assert failure.signature == (
        "mcp-statecheck:v1:"
        "ee7cd128d46782066b1f3b31250651281bd6bb104ae06f46960543b5c91d0ab1"
    )


def test_duplicate_request_signature_ignores_internal_and_wire_ids() -> None:
    def signature_for(prefix: str, request_id: int) -> str:
        failure = detect_failure(
            (
                *_initialized_session(prefix, request_id + 1),
                Action(
                    f"{prefix}-a",
                    ActionKind.REQUEST,
                    mcp_request_id=request_id,
                    method="tools/call",
                ),
                Action(
                    f"{prefix}-b",
                    ActionKind.REQUEST,
                    mcp_request_id=request_id,
                    method="tools/call",
                ),
            ),
            (),
        )
        assert failure is not None
        return failure.signature

    assert signature_for("one", 7) == signature_for("other", 900)


def test_completed_request_id_still_cannot_be_reused_in_the_same_session() -> None:
    failure = detect_failure(
        (
            *_initialized_session(),
            Action(
                "call-a",
                ActionKind.REQUEST,
                mcp_request_id=7,
                method="tools/call",
            ),
            Action(
                "call-a-response",
                ActionKind.RESPONSE,
                target_action_id="call-a",
            ),
            Action(
                "call-b",
                ActionKind.REQUEST,
                mcp_request_id=7,
                method="tools/call",
            ),
        ),
        (),
    )

    assert failure is not None
    assert failure.evidence["overlap"] == "completed"


def test_integer_and_string_request_ids_are_distinct() -> None:
    failure = detect_failure(
        (
            *_initialized_session(),
            Action(
                "integer-id",
                ActionKind.REQUEST,
                mcp_request_id=7,
                method="tools/call",
            ),
            Action(
                "string-id",
                ActionKind.REQUEST,
                mcp_request_id="7",
                method="tools/call",
            ),
        ),
        (),
    )

    assert failure is None


def test_duplicate_ids_without_a_session_are_not_classified_as_same_session() -> None:
    failure = detect_failure(
        (
            Action(
                "call-a",
                ActionKind.REQUEST,
                mcp_request_id=7,
                method="tools/call",
            ),
            Action(
                "call-b",
                ActionKind.REQUEST,
                mcp_request_id=7,
                method="tools/call",
            ),
        ),
        (),
    )

    assert failure is None


def test_invalid_boolean_ids_are_not_classified_as_duplicate_request_ids() -> None:
    failure = detect_failure(
        (
            *_initialized_session(),
            Action(
                "boolean-a",
                ActionKind.REQUEST,
                mcp_request_id=True,
                method="tools/call",
            ),
            Action(
                "boolean-b",
                ActionKind.REQUEST,
                mcp_request_id=True,
                method="tools/call",
            ),
        ),
        (),
    )

    assert failure is None


@pytest.mark.parametrize(
    "boundary",
    (
        ActionKind.CONNECT,
        ActionKind.RECONNECT,
        ActionKind.SESSION_EXPIRED,
        ActionKind.DISCONNECT,
        ActionKind.CLOSE,
    ),
)
def test_request_ids_can_be_reused_after_a_session_boundary(
    boundary: ActionKind,
) -> None:
    failure = detect_failure(
        (
            *_initialized_session(),
            Action(
                "call-before-boundary",
                ActionKind.REQUEST,
                mcp_request_id=7,
                method="tools/call",
            ),
            Action("boundary", boundary),
            Action(
                "next-initialize",
                ActionKind.INITIALIZE,
                mcp_request_id=7,
                protocol_version="2025-11-25",
            ),
        ),
        (),
    )

    assert failure is None


def test_stream_resume_does_not_clear_request_id_history() -> None:
    failure = detect_failure(
        (
            *_initialized_session(),
            Action(
                "call-before-resume",
                ActionKind.REQUEST,
                mcp_request_id=7,
                method="tools/call",
            ),
            Action("resume", ActionKind.RESUME_STREAM),
            Action(
                "call-after-resume",
                ActionKind.REQUEST,
                mcp_request_id=7,
                method="tools/call",
            ),
        ),
        (),
    )

    assert failure is not None
    assert failure.kind == "messages.request_id_reused_within_session"


def test_cancelled_result_matched_to_later_request_is_a_stable_failure() -> None:
    failure = detect_failure(
        _late_cancellation_actions(),
        _late_correlation_events(),
        fixture_id=LATE_RESPONSE_FIXTURE_ID,
    )

    assert failure is not None
    assert failure.kind == "differential.response_correlated_to_wrong_request"
    assert failure.trigger_action_id == "call-b"
    assert failure.evidence == {
        "cancellation_context": "after_cancellation",
        "correlation": "wrong_request",
        "direction": "server_to_client",
        "expected_canary": "call-b",
        "observed_canary": "call-a",
        "oracle": "fixture_canary",
        "response_mcp_request_id": 22,
        "source_action_id": "call-a",
        "source_request_status": "cancelled",
        "subject": "server",
        "target_action_id": "call-b",
    }
    assert failure.signature == (
        "mcp-statecheck:v1:"
        "a1107b3b1e4ddfa1a9d16bd82f94810c77e6b43c7eee8844be089a4c7550ff1c"
    )


def test_fixture_canary_oracle_requires_explicit_fixture_scope() -> None:
    failure = detect_failure(
        _late_cancellation_actions(),
        _late_correlation_events(),
    )

    assert failure is None


def test_canary_oracle_requires_cancel_id_to_match_source_request() -> None:
    actions = list(_late_cancellation_actions())
    actions[4] = replace(actions[4], mcp_request_id=999)

    failure = detect_failure(
        actions,
        _late_correlation_events(),
        fixture_id=LATE_RESPONSE_FIXTURE_ID,
    )

    assert failure is None


def test_reciprocal_canary_swap_is_independent_of_response_order() -> None:
    base_actions = _late_cancellation_actions()
    actions = (*base_actions[:-2], base_actions[-1], base_actions[-2])
    base_events = _late_correlation_events()
    events = (base_events[0], base_events[2], base_events[1])

    failure = detect_failure(
        actions,
        events,
        fixture_id=LATE_RESPONSE_FIXTURE_ID,
    )

    assert failure is not None
    assert failure.kind == "differential.response_correlated_to_wrong_request"


def test_correct_late_response_correlation_is_not_a_failure() -> None:
    actions = (
        *_late_cancellation_actions()[:-2],
        Action("late-response", ActionKind.RESPONSE, target_action_id="call-a"),
        Action("current-response", ActionKind.RESPONSE, target_action_id="call-b"),
    )
    failure = detect_failure(
        actions,
        (
            {
                "kind": "response",
                "mcp_request_id": 1,
                "payload": {},
                "target_action_id": "session-initialize",
            },
            {
                "kind": "response",
                "mcp_request_id": 21,
                "payload": {
                    "content": [],
                    "structuredContent": {"fixtureCanary": "call-a"},
                },
                "target_action_id": "call-a",
            },
            {
                "kind": "response",
                "mcp_request_id": 22,
                "payload": {
                    "content": [],
                    "structuredContent": {"fixtureCanary": "call-b"},
                },
                "target_action_id": "call-b",
            },
        ),
        fixture_id=LATE_RESPONSE_FIXTURE_ID,
    )

    assert failure is None


def test_correlation_signature_ignores_wire_ids_and_canary_values() -> None:
    def signature_for(first_id: int, second_id: int, first: str, second: str) -> str:
        actions = list(_late_cancellation_actions())
        actions[3] = replace(
            actions[3],
            mcp_request_id=first_id,
            payload={
                "arguments": {"fixtureCanary": first},
                "name": "fixture",
            },
        )
        actions[4] = replace(actions[4], mcp_request_id=first_id)
        actions[5] = replace(
            actions[5],
            mcp_request_id=second_id,
            payload={
                "arguments": {"fixtureCanary": second},
                "name": "fixture",
            },
        )
        failure = detect_failure(
            actions,
            (
                {
                    "kind": "response",
                    "mcp_request_id": 1,
                    "payload": {},
                    "target_action_id": "session-initialize",
                },
                {
                    "kind": "response",
                    "mcp_request_id": second_id,
                    "payload": {
                        "content": [],
                        "structuredContent": {"fixtureCanary": first},
                    },
                    "target_action_id": "call-b",
                },
                {
                    "kind": "response",
                    "mcp_request_id": first_id,
                    "payload": {
                        "content": [],
                        "structuredContent": {"fixtureCanary": second},
                    },
                    "target_action_id": "call-a",
                },
            ),
            fixture_id=LATE_RESPONSE_FIXTURE_ID,
        )
        assert failure is not None
        return failure.signature

    assert signature_for(21, 22, "call-a", "call-b") == signature_for(
        700,
        900,
        "first-canary",
        "second-canary",
    )
