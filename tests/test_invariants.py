import pytest

from mcp_statecheck.invariants import detect_failure
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
