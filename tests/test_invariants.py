from dataclasses import replace

import pytest

from mcp_statecheck.fixtures import HTTP_ERROR_FIXTURE_ID, SSE_RESUME_FIXTURE_ID
from mcp_statecheck.invariants import (
    LATE_RESPONSE_FIXTURE_ID,
    detect_failure,
)
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


def _http_error_actions(prefix: str = "") -> tuple[Action, ...]:
    return (
        Action(f"{prefix}connect", ActionKind.CONNECT),
        Action(
            f"{prefix}initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
        ),
    )


def _http_timeout_event(
    *, status: object = 503, target_action_id: str = "initialize"
) -> dict[str, object]:
    return {
        "kind": "timeout",
        "fixture_source_kind": "http_error",
        "http_method": "POST",
        "status": status,
        "target_action_id": target_action_id,
    }


def _sse_resume_actions(
    *,
    prefix: str = "",
    stream_id: str = "server-events",
    first_token: str = "cursor-1",
    second_token: str = "cursor-2",
) -> tuple[Action, ...]:
    return (
        Action(
            f"{prefix}initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
        ),
        Action(f"{prefix}initialized", ActionKind.INITIALIZED),
        Action(
            f"{prefix}open-sse",
            ActionKind.OPEN_STREAM,
            stream_id=stream_id,
        ),
        Action(
            f"{prefix}resume-1",
            ActionKind.RESUME_STREAM,
            stream_id=stream_id,
            resume_token=first_token,
        ),
        Action(
            f"{prefix}resume-2",
            ActionKind.RESUME_STREAM,
            stream_id=stream_id,
            resume_token=second_token,
        ),
    )


def _sse_resume_events(actions: tuple[Action, ...]) -> tuple[dict[str, object], ...]:
    initialize, _, opened, first_resume, second_resume = actions
    return (
        {
            "kind": "response",
            "outcome": "success",
            "target_action_id": initialize.action_id,
        },
        {
            "kind": "sse_resume",
            "peer_protocol_version": "2025-11-25",
            "received_event_id": first_resume.resume_token,
            "peer_last_event_id": None,
            "peer_session_id": "fixture-session",
            "sent_last_event_id": None,
            "session_id": "fixture-session",
            "stream_id": opened.stream_id,
            "target_action_id": opened.action_id,
        },
        {
            "kind": "sse_resume",
            "peer_protocol_version": "2025-11-25",
            "received_event_id": second_resume.resume_token,
            "peer_last_event_id": first_resume.resume_token,
            "peer_session_id": "fixture-session",
            "sent_last_event_id": first_resume.resume_token,
            "session_id": "fixture-session",
            "stream_id": first_resume.stream_id,
            "target_action_id": first_resume.action_id,
        },
        {
            "kind": "sse_resume",
            "peer_protocol_version": "2025-11-25",
            "received_event_id": "cursor-after-loss",
            "peer_last_event_id": None,
            "peer_session_id": "fixture-session",
            "sent_last_event_id": "",
            "session_id": "fixture-session",
            "stream_id": second_resume.stream_id,
            "target_action_id": second_resume.action_id,
        },
    )


def test_http_error_normalized_as_timeout_is_a_stable_failure() -> None:
    failure = detect_failure(
        _http_error_actions(),
        (_http_timeout_event(),),
        fixture_id=HTTP_ERROR_FIXTURE_ID,
    )

    assert failure is not None
    assert failure.kind == "differential.http_status_reported_as_timeout"
    assert failure.trigger_action_id == "initialize"
    assert failure.evidence == {
        "direction": "server_to_client",
        "http_method": "POST",
        "http_status": 503,
        "oracle": "fixture_http_status",
        "reported_kind": "timeout",
        "source_kind": "http_error",
        "status_class": "5xx",
        "subject": "client_adapter",
        "target_action_id": "initialize",
    }
    assert failure.signature == (
        "mcp-statecheck:v1:"
        "7aa9b18ce897c3493f9fdd550fd442f131bf7d8a6e5a8b46a0460cbd248b1ce0"
    )


@pytest.mark.parametrize(
    "event",
    (
        {**_http_timeout_event(), "kind": "http_error"},
        {**_http_timeout_event(), "fixture_source_kind": "timeout"},
        _http_timeout_event(status=200),
        _http_timeout_event(status=True),
        {**_http_timeout_event(), "http_method": "GET"},
        _http_timeout_event(target_action_id="connect"),
    ),
)
def test_http_status_timeout_oracle_rejects_untrusted_evidence(
    event: dict[str, object],
) -> None:
    assert (
        detect_failure(
            _http_error_actions(),
            (event,),
            fixture_id=HTTP_ERROR_FIXTURE_ID,
        )
        is None
    )


def test_http_status_timeout_oracle_requires_fixture_scope_and_connect() -> None:
    event = _http_timeout_event()
    assert detect_failure(_http_error_actions(), (event,)) is None
    assert (
        detect_failure(
            _http_error_actions()[1:],
            (event,),
            fixture_id=HTTP_ERROR_FIXTURE_ID,
        )
        is None
    )
    reinitialized = (
        Action("connect", ActionKind.CONNECT),
        Action(
            "first-initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
        ),
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=2,
            protocol_version="2025-11-25",
        ),
    )
    assert (
        detect_failure(
            reinitialized,
            (event,),
            fixture_id=HTTP_ERROR_FIXTURE_ID,
        )
        is None
    )


def test_http_status_timeout_signature_ignores_status_and_action_ids() -> None:
    first = detect_failure(
        _http_error_actions(),
        (_http_timeout_event(status=500),),
        fixture_id=HTTP_ERROR_FIXTURE_ID,
    )
    second = detect_failure(
        _http_error_actions("other-"),
        (
            _http_timeout_event(
                status=599,
                target_action_id="other-initialize",
            ),
        ),
        fixture_id=HTTP_ERROR_FIXTURE_ID,
    )
    assert first is not None
    assert second is not None
    assert first.signature == second.signature


def test_second_sse_reconnect_with_missing_header_is_a_stable_failure() -> None:
    actions = _sse_resume_actions()
    failure = detect_failure(
        actions,
        _sse_resume_events(actions),
        fixture_id=SSE_RESUME_FIXTURE_ID,
    )

    assert failure is not None
    assert failure.kind == "differential.sse_resume_token_lost"
    assert failure.trigger_action_id == "resume-2"
    assert failure.evidence == {
        "direction": "client_to_server",
        "expected_last_event_id": "cursor-2",
        "last_event_id_state": "missing",
        "observed_last_event_id": None,
        "oracle": "fixture_sse_header",
        "reconnect": "subsequent",
        "reported_last_event_id": "",
        "resume_ordinal": 2,
        "session_scope": "same_session",
        "stream_id": "server-events",
        "stream_scope": "same_stream",
        "subject": "client_adapter",
        "target_action_id": "resume-2",
        "transport": "streamable_http",
    }
    assert failure.signature == (
        "mcp-statecheck:v1:"
        "0f243dca5ddedb72115708cc6ddeb547817b647afa0a13a972db1ce44de74ff9"
    )


def test_sse_resume_oracle_requires_fixture_scope_and_complete_chain() -> None:
    actions = _sse_resume_actions()
    events = _sse_resume_events(actions)
    response, *sse_events = events
    assert detect_failure(actions, events) is None

    incomplete = (response, *sse_events[:-1])
    missing_initialize_response = tuple(sse_events)
    wrong_first_header = (
        response,
        {**sse_events[0], "peer_last_event_id": "cursor-0"},
        *sse_events[1:],
    )
    correct_second_header = (
        response,
        *sse_events[:2],
        {
            **sse_events[2],
            "peer_last_event_id": "cursor-2",
            "sent_last_event_id": "cursor-2",
        },
    )
    wrong_target_type = (
        response,
        *sse_events[:2],
        {**sse_events[2], "target_action_id": "initialize"},
    )
    wrong_stream = (
        response,
        *sse_events[:2],
        {**sse_events[2], "stream_id": "other-stream"},
    )
    missing_peer_header = (
        response,
        *sse_events[:2],
        {
            key: value
            for key, value in sse_events[2].items()
            if key != "peer_last_event_id"
        },
    )
    for candidate in (
        incomplete,
        missing_initialize_response,
        wrong_first_header,
        correct_second_header,
        wrong_target_type,
        wrong_stream,
        missing_peer_header,
    ):
        assert (
            detect_failure(
                actions,
                candidate,
                fixture_id=SSE_RESUME_FIXTURE_ID,
            )
            is None
        )


def test_sse_resume_oracle_requires_same_session_and_action_types() -> None:
    actions = _sse_resume_actions()
    events = _sse_resume_events(actions)
    response, *sse_events = events
    across_reconnect = (
        *actions[:4],
        Action("connection-boundary", ActionKind.RECONNECT),
        actions[4],
    )
    across_reinitialize = (
        *actions[:4],
        Action(
            "next-initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=2,
            protocol_version="2025-11-25",
        ),
        actions[4],
    )
    wrong_type = (
        *actions[:4],
        replace(actions[4], kind=ActionKind.OPEN_STREAM),
    )
    missing_initialized = (actions[0], *actions[2:])
    changed_peer_session = (
        response,
        sse_events[0],
        {**sse_events[1], "peer_session_id": "other-session"},
        sse_events[2],
    )
    changed_protocol = (
        response,
        sse_events[0],
        {**sse_events[1], "peer_protocol_version": "2025-06-18"},
        sse_events[2],
    )
    assert (
        detect_failure(
            across_reconnect,
            events,
            fixture_id=SSE_RESUME_FIXTURE_ID,
        )
        is None
    )
    assert (
        detect_failure(
            wrong_type,
            events,
            fixture_id=SSE_RESUME_FIXTURE_ID,
        )
        is None
    )
    assert (
        detect_failure(
            across_reinitialize,
            events,
            fixture_id=SSE_RESUME_FIXTURE_ID,
        )
        is None
    )
    for candidate_actions, candidate_events in (
        (missing_initialized, events),
        (actions, changed_peer_session),
        (actions, changed_protocol),
    ):
        assert (
            detect_failure(
                candidate_actions,
                candidate_events,
                fixture_id=SSE_RESUME_FIXTURE_ID,
            )
            is None
        )


def test_sse_resume_oracle_trusts_peer_headers_and_ignores_unrelated_events() -> None:
    actions = _sse_resume_actions()
    events = _sse_resume_events(actions)
    response, *sse_events = events
    client_claims_cursor = (
        response,
        *sse_events[:2],
        {
            **sse_events[2],
            "sent_last_event_id": "cursor-2",
            "session_id": "client-only-session",
        },
        {
            **sse_events[0],
            "stream_id": "unrelated",
            "target_action_id": "unrelated-action",
        },
    )

    failure = detect_failure(
        actions,
        client_claims_cursor,
        fixture_id=SSE_RESUME_FIXTURE_ID,
    )

    assert failure is not None
    assert failure.evidence["reported_last_event_id"] == "cursor-2"


def test_sse_resume_signature_ignores_tokens_stream_and_action_ids() -> None:
    first_actions = _sse_resume_actions()
    second_actions = _sse_resume_actions(
        prefix="other-",
        stream_id="secondary",
        first_token="alpha",
        second_token="beta",
    )
    first = detect_failure(
        first_actions,
        _sse_resume_events(first_actions),
        fixture_id=SSE_RESUME_FIXTURE_ID,
    )
    second = detect_failure(
        second_actions,
        _sse_resume_events(second_actions),
        fixture_id=SSE_RESUME_FIXTURE_ID,
    )
    assert first is not None
    assert second is not None
    assert first.signature == second.signature
