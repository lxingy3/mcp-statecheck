from mcp_statecheck.model import (
    Action,
    ActionKind,
    LifecyclePhase,
    ModelState,
    RequestStatus,
    reduce,
)


def test_lifecycle_and_negotiation_are_explicit() -> None:
    state = reduce(ModelState(), Action("connect", ActionKind.CONNECT))
    assert state.phase is LifecyclePhase.CONNECTED

    state = reduce(
        state,
        Action(
            "init",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
            capabilities={"sampling": {}, "roots": {"listChanged": True}},
        ),
    )
    assert state.phase is LifecyclePhase.INITIALIZING
    assert state.requested_protocol_version == "2025-11-25"
    assert state.client_capabilities == {
        "roots": {"listChanged": True},
        "sampling": {},
    }

    state = reduce(
        state,
        Action(
            "init-response",
            ActionKind.RESPONSE,
            mcp_request_id=1,
            target_action_id="init",
            protocol_version="2025-06-18",
            capabilities={"tools": {}},
        ),
    )
    state = reduce(state, Action("ready", ActionKind.INITIALIZED))
    assert state.phase is LifecyclePhase.INITIALIZED
    assert state.negotiated_protocol_version == "2025-06-18"
    assert state.server_capabilities == {"tools": {}}

    state = reduce(state, Action("expired", ActionKind.SESSION_EXPIRED))
    assert state.phase is LifecyclePhase.CONNECTED
    assert state.requested_protocol_version is None
    assert state.negotiated_protocol_version is None
    assert state.client_capabilities is None
    assert state.server_capabilities is None
    state = reduce(state, Action("disconnect", ActionKind.DISCONNECT))
    assert state.phase is LifecyclePhase.DISCONNECTED
    state = reduce(state, Action("close", ActionKind.CLOSE))
    assert state.phase is LifecyclePhase.CLOSED


def test_duplicate_mcp_ids_are_preserved_and_never_guessed() -> None:
    state = ModelState(phase=LifecyclePhase.INITIALIZED)
    state = reduce(
        state,
        Action("call-a", ActionKind.REQUEST, mcp_request_id=7, method="tools/call"),
    )
    state = reduce(
        state,
        Action("call-b", ActionKind.REQUEST, mcp_request_id=7, method="tools/call"),
    )

    state = reduce(state, Action("ambiguous", ActionKind.RESPONSE, mcp_request_id=7))
    assert [request.action_id for request in state.requests] == ["call-a", "call-b"]
    assert all(request.status is RequestStatus.PENDING for request in state.requests)
    assert state.unmatched_response_action_ids == ("ambiguous",)

    state = reduce(
        state,
        Action(
            "response-a",
            ActionKind.RESPONSE,
            mcp_request_id=7,
            target_action_id="call-a",
        ),
    )
    state = reduce(
        state,
        Action(
            "cancel-b",
            ActionKind.CANCEL,
            mcp_request_id=7,
            target_action_id="call-b",
        ),
    )
    state = reduce(
        state,
        Action(
            "late-b",
            ActionKind.RESPONSE,
            mcp_request_id=7,
            target_action_id="call-b",
        ),
    )
    assert state.request("call-a").status is RequestStatus.COMPLETED  # type: ignore[union-attr]
    assert state.request("call-b").status is RequestStatus.LATE_RESPONSE  # type: ignore[union-attr]


def test_even_a_unique_mcp_id_never_correlates_a_response() -> None:
    state = reduce(
        ModelState(phase=LifecyclePhase.INITIALIZED),
        Action("call", ActionKind.REQUEST, mcp_request_id=7, method="ping"),
    )
    state = reduce(state, Action("uncorrelated", ActionKind.RESPONSE, mcp_request_id=7))

    assert state.request("call").status is RequestStatus.PENDING  # type: ignore[union-attr]
    assert state.unmatched_response_action_ids == ("uncorrelated",)

    state = reduce(
        state,
        Action(
            "correlated",
            ActionKind.RESPONSE,
            mcp_request_id="a deliberately different wire ID",
            target_action_id="call",
        ),
    )
    assert state.request("call").status is RequestStatus.COMPLETED  # type: ignore[union-attr]


def test_near_valid_request_is_recorded_before_initialization() -> None:
    state = reduce(
        ModelState(phase=LifecyclePhase.CONNECTED),
        Action(
            "early-list",
            ActionKind.REQUEST,
            mcp_request_id="same-id",
            method="tools/list",
        ),
    )
    assert state.phase is LifecyclePhase.CONNECTED
    assert state.request("early-list").status is RequestStatus.PENDING  # type: ignore[union-attr]


def test_stream_disconnect_and_resume_are_first_class_actions() -> None:
    state = ModelState(phase=LifecyclePhase.INITIALIZED)
    state = reduce(
        state,
        Action(
            "open",
            ActionKind.OPEN_STREAM,
            stream_id="server-events",
            resume_token="cursor-1",
        ),
    )
    state = reduce(
        state,
        Action(
            "disconnect",
            ActionKind.DISCONNECT_STREAM,
            stream_id="server-events",
            resume_token="cursor-1",
        ),
    )
    state = reduce(
        state,
        Action(
            "resume",
            ActionKind.RESUME_STREAM,
            stream_id="server-events",
            resume_token="cursor-1",
        ),
    )

    assert state.phase is LifecyclePhase.INITIALIZED
    assert [action.kind for action in state.actions] == [
        ActionKind.OPEN_STREAM,
        ActionKind.DISCONNECT_STREAM,
        ActionKind.RESUME_STREAM,
    ]
    assert ModelState.from_dict(state.to_dict()) == state


def test_new_connection_cannot_inherit_negotiation() -> None:
    negotiated = ModelState(
        phase=LifecyclePhase.INITIALIZED,
        requested_protocol_version="2025-11-25",
        negotiated_protocol_version="2025-06-18",
        client_capabilities={"roots": {}},
        server_capabilities={"tools": {}},
    )

    disconnected = reduce(negotiated, Action("disconnect", ActionKind.DISCONNECT))
    reconnected = reduce(disconnected, Action("reconnect", ActionKind.RECONNECT))

    assert reconnected.phase is LifecyclePhase.CONNECTED
    assert reconnected.requested_protocol_version is None
    assert reconnected.negotiated_protocol_version is None
    assert reconnected.client_capabilities is None
    assert reconnected.server_capabilities is None


def test_model_round_trip_is_deterministic() -> None:
    state = reduce(
        ModelState(),
        Action(
            "request",
            ActionKind.REQUEST,
            mcp_request_id="r1",
            method="ping",
            payload={"z": 1, "a": {"y": 2, "b": 3}},
        ),
    )
    encoded = state.to_dict()
    restored = ModelState.from_dict(encoded)
    assert restored == state
    assert restored.to_dict() == encoded
