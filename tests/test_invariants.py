from mcp_statecheck.invariants import detect_failure
from mcp_statecheck.model import Action, ActionKind


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
