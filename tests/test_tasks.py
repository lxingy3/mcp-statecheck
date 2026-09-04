"""Independent checks of observed modern task lifecycles."""

import pytest

from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.tasks import (
    PROTOCOL_VERSION,
    TASK_CAPABILITIES,
    evaluate_tasks,
)


def request(
    action_id, method="tools/call", *, target=None, capabilities=None, payload=None
):
    return Action(
        action_id,
        ActionKind.REQUEST,
        mcp_request_id=action_id,
        method=method,
        target_action_id=target,
        protocol_version=PROTOCOL_VERSION,
        capabilities=TASK_CAPABILITIES if capabilities is None else capabilities,
        payload={} if payload is None else payload,
    )


def task(status="working", *, creation=False, task_id="opaque-task", **fields):
    return {
        "resultType": "task" if creation else "complete",
        "taskId": task_id,
        "status": status,
        "createdAt": "2026-09-04T12:00:00Z",
        "lastUpdatedAt": "2026-09-04T12:00:00Z",
        "ttlMs": None,
        **fields,
    }


def response(action_id, payload, *, outcome="success"):
    return {
        "kind": "response",
        "target_action_id": action_id,
        "mcp_request_id": action_id,
        "outcome": outcome,
        "payload": payload,
    }


def test_completed_tool_error_is_a_valid_completed_task():
    result = evaluate_tasks(
        [request("start"), request("poll", "tasks/get", target="start")],
        [
            response("start", task(creation=True)),
            response(
                "poll",
                task("completed", result={"content": [], "isError": True}),
            ),
        ],
    )
    assert result.failure is None
    assert result.transitions == ("created->working", "working->completed")
    assert result.task_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"resultType": "task"},
        {"status": "unknown"},
        {"taskId": ""},
        {"createdAt": "not-a-time"},
        {"ttlMs": True},
        {"ttlMs": -1},
        {"pollIntervalMs": 0.5},
        {"status": "completed"},
        {"status": "completed", "result": []},
        {"status": "failed", "result": {"isError": True}},
        {"status": "failed", "error": {"code": True, "message": "error"}},
        {"status": "input_required", "inputRequests": []},
    ],
)
def test_invalid_detailed_task_is_a_server_failure(changes):
    result = evaluate_tasks(
        [request("start"), request("poll", "tasks/get", target="start")],
        [response("start", task(creation=True)), response("poll", task(**changes))],
    )
    assert result.failure is not None
    assert result.failure.kind == "tasks.invalid_result"
    assert result.failure.trigger_action_id == "poll"


@pytest.mark.parametrize(
    ("previous", "current", "kind"),
    [
        (task(creation=True), task(task_id="another-task"), "tasks.identity_changed"),
        (
            task("completed", creation=True),
            task("working"),
            "tasks.terminal_state_changed",
        ),
        (
            task("cancelled", creation=True),
            task("completed", result={"content": []}),
            "tasks.terminal_state_changed",
        ),
        (
            task("completed", creation=True),
            task("failed", error={"code": -32603, "message": "broken"}),
            "tasks.terminal_state_changed",
        ),
    ],
)
def test_task_identity_and_terminal_status_are_stable(previous, current, kind):
    result = evaluate_tasks(
        [request("start"), request("poll", "tasks/get", target="start")],
        [response("start", previous), response("poll", current)],
    )
    assert result.failure is not None
    assert result.failure.kind == kind


def test_terminal_task_may_fail_after_its_ttl_elapsed():
    result = evaluate_tasks(
        [request("start"), request("poll", "tasks/get", target="start")],
        [
            response("start", task("completed", creation=True, ttlMs=1000)),
            response(
                "poll",
                task(
                    "failed",
                    ttlMs=1000,
                    lastUpdatedAt="2026-09-04T12:00:01Z",
                    error={"code": -32603, "message": "Task expired"},
                ),
            ),
        ],
    )
    assert result.failure is None


@pytest.mark.parametrize(
    "method", ["tools/call", "tasks/get", "tasks/update", "tasks/cancel"]
)
def test_task_responses_require_capability_on_that_request(method):
    actions = [
        request("start"),
        request("operation", method, target="start", capabilities={}),
    ]
    payload = task(creation=method == "tools/call")
    if method in {"tasks/update", "tasks/cancel"}:
        payload = {"resultType": "complete"}
    result = evaluate_tasks(
        actions,
        [response("start", task(creation=True)), response("operation", payload)],
    )
    assert result.failure is not None
    assert result.failure.kind == "tasks.capability_not_declared"


@pytest.mark.parametrize("method", ["tasks/update", "tasks/cancel"])
def test_task_acknowledgements_are_empty_complete_results(method):
    result = evaluate_tasks(
        [request("start"), request("ack", method, target="start")],
        [response("start", task(creation=True)), response("ack", task())],
    )
    assert result.failure is not None
    assert result.failure.kind == "tasks.invalid_acknowledgement"


def input_request(message="Please enter a value."):
    return {
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": message,
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }


def test_input_key_cannot_change_meaning_between_polls():
    result = evaluate_tasks(
        [
            request("start"),
            request("first", "tasks/get", target="start"),
            request("second", "tasks/get", target="start"),
        ],
        [
            response("start", task(creation=True)),
            response(
                "first", task("input_required", inputRequests={"key": input_request()})
            ),
            response(
                "second",
                task(
                    "input_required",
                    inputRequests={"key": input_request("A different question.")},
                ),
            ),
        ],
    )
    assert result.failure is not None
    assert result.failure.kind == "tasks.input_request_key_reused"


@pytest.mark.parametrize(
    "inputs",
    [
        {"key": {}},
        {"key": {"method": "unknown", "params": {}}},
        {"key": {"method": "elicitation/create", "params": []}},
        {"key": {"method": ["elicitation/create"], "params": {}}},
    ],
)
def test_invalid_input_requests_are_rejected(inputs):
    result = evaluate_tasks(
        [request("start"), request("poll", "tasks/get", target="start")],
        [
            response("start", task(creation=True)),
            response("poll", task("input_required", inputRequests=inputs)),
        ],
    )
    assert result.failure is not None
    assert result.failure.kind == "tasks.invalid_result"


@pytest.mark.parametrize("code", [-32602, -32603])
def test_missing_capability_requires_the_specific_protocol_error(code):
    result = evaluate_tasks(
        [request("poll", "tasks/get", capabilities={})],
        [response("poll", {"code": code, "message": "Failure"}, outcome="error")],
    )
    assert result.failure is not None
    assert result.failure.kind == "tasks.capability_not_declared"


def test_cancel_acknowledgement_does_not_guarantee_cancellation():
    actions = [
        request("start"),
        request("cancel", "tasks/cancel", target="start"),
        request("poll", "tasks/get", target="start"),
        request("finish", "tasks/get", target="start"),
    ]
    events = [
        response("start", task(creation=True)),
        response("cancel", {"resultType": "complete", "_meta": {}}),
        response("poll", task()),
        response("finish", task("completed", result={"content": []})),
    ]
    assert evaluate_tasks(actions, events).failure is None


def test_update_acknowledgement_allows_unchanged_stale_input_poll():
    actions = [
        request("start"),
        request("first", "tasks/get", target="start"),
        request(
            "update",
            "tasks/update",
            target="start",
            payload={"inputResponses": {"key": {"action": "accept"}}},
        ),
        request("second", "tasks/get", target="start"),
    ]
    pending = task("input_required", inputRequests={"key": input_request()})
    events = [
        response("start", task(creation=True)),
        response("first", pending),
        response("update", {"resultType": "complete"}),
        response("second", pending),
    ]
    assert evaluate_tasks(actions, events).failure is None


@pytest.mark.parametrize(
    "status", ["working", "input_required", "completed", "cancelled", "failed"]
)
def test_creation_is_a_base_task_and_may_begin_in_any_status(status):
    result = evaluate_tasks(
        [request("start")], [response("start", task(status, creation=True))]
    )
    assert result.failure is None
    assert result.task_count == 1


@pytest.mark.parametrize(
    ("method", "capabilities", "payload", "outcome"),
    [
        (
            "tools/call",
            TASK_CAPABILITIES,
            {"resultType": "complete", "content": []},
            "success",
        ),
        ("tools/call", {}, {"resultType": "complete", "content": []}, "success"),
        (
            "tools/call",
            TASK_CAPABILITIES,
            {"resultType": "input_required", "requestState": "opaque"},
            "success",
        ),
        ("tasks/get", {}, {"code": -32021, "message": "Missing capability"}, "error"),
        (
            "tasks/get",
            TASK_CAPABILITIES,
            {"code": -32602, "message": "Task expired"},
            "error",
        ),
    ],
)
def test_optional_tasks_and_protocol_errors_do_not_invent_failures(
    method, capabilities, payload, outcome
):
    result = evaluate_tasks(
        [request("call", method, capabilities=capabilities)],
        [response("call", payload, outcome=outcome)],
    )
    assert result.failure is None
    assert result.task_count == 0


def test_unrelated_notifications_and_planned_responses_are_not_server_evidence():
    actions = [
        request("start"),
        Action(
            "planned",
            ActionKind.RESPONSE,
            target_action_id="start",
            payload=task(creation=True),
        ),
    ]
    events = [
        response("unknown", task(creation=True)),
        {"kind": "notification", "payload": task("completed")},
    ]
    result = evaluate_tasks(actions, events)
    assert result.failure is None
    assert result.task_count == 0


def test_answered_input_key_cannot_reappear_after_observed_retirement():
    actions = [
        request("start"),
        request("first", "tasks/get", target="start"),
        request(
            "update",
            "tasks/update",
            target="start",
            payload={"inputResponses": {"key": {"action": "accept"}}},
        ),
        request("clear", "tasks/get", target="start"),
        request("second", "tasks/get", target="start"),
    ]
    pending = task("input_required", inputRequests={"key": input_request()})
    events = [
        response("start", task(creation=True)),
        response("first", pending),
        response("update", {"resultType": "complete"}),
        response("clear", task()),
        response("second", pending),
    ]
    result = evaluate_tasks(actions, events)
    assert result.failure is not None
    assert result.failure.kind == "tasks.input_request_key_reused"


def test_ignored_unknown_input_response_does_not_reserve_a_future_key():
    actions = [
        request("start"),
        request(
            "update",
            "tasks/update",
            target="start",
            payload={"inputResponses": {"future": {"action": "accept"}}},
        ),
        request("working", "tasks/get", target="start"),
        request("input", "tasks/get", target="start"),
    ]
    events = [
        response("start", task(creation=True)),
        response("update", {"resultType": "complete"}),
        response("working", task()),
        response(
            "input", task("input_required", inputRequests={"future": input_request()})
        ),
    ]
    assert evaluate_tasks(actions, events).failure is None


@pytest.mark.parametrize("payload", [None, [], 3, "task"])
@pytest.mark.parametrize(
    "method", ["tools/call", "tasks/get", "tasks/update", "tasks/cancel"]
)
def test_successful_task_responses_must_be_objects(payload, method):
    result = evaluate_tasks([request("call", method)], [response("call", payload)])
    assert result.failure is not None
    assert result.failure.kind == "tasks.invalid_result"


@pytest.mark.parametrize("method", ["ping", "tools/list"])
def test_unsupported_method_cannot_create_a_task(method):
    result = evaluate_tasks(
        [request("call", method)], [response("call", task(creation=True))]
    )
    assert result.failure is not None
    assert result.failure.kind == "tasks.unsupported_creation_method"


def test_task_fields_do_not_replace_the_creation_discriminator():
    result = evaluate_tasks([request("call")], [response("call", task())])
    assert result.failure is not None
    assert result.failure.kind == "tasks.invalid_result"


def test_shared_durable_task_handle_is_counted_once():
    result = evaluate_tasks(
        [request("first"), request("second")],
        [
            response("first", task(creation=True)),
            response("second", task(creation=True)),
        ],
    )
    assert result.failure is None
    assert result.task_count == 1


def test_task_input_requests_require_corresponding_client_capability():
    result = evaluate_tasks(
        [
            request("start"),
            request(
                "poll",
                "tasks/get",
                target="start",
                capabilities={"extensions": {"io.modelcontextprotocol/tasks": {}}},
            ),
        ],
        [
            response("start", task(creation=True)),
            response(
                "poll", task("input_required", inputRequests={"key": input_request()})
            ),
        ],
    )
    assert result.failure is not None
    assert result.failure.kind == "tasks.input_capability_not_declared"


@pytest.mark.parametrize(
    "fields",
    [
        {"inputRequests": {"key": "not-a-request"}},
        {"error": []},
        {"result": []},
        {"_meta": []},
    ],
)
def test_optional_fields_are_well_formed_when_present(fields):
    result = evaluate_tasks(
        [request("start")], [response("start", task(creation=True, **fields))]
    )
    assert result.failure is not None
    assert result.failure.kind == "tasks.invalid_result"


def test_shared_handle_keeps_one_observed_lifecycle_across_action_bindings():
    result = evaluate_tasks(
        [
            request("first"),
            request("second"),
            request("done", "tasks/get", target="first"),
            request("regression", "tasks/get", target="second"),
        ],
        [
            response("first", task(creation=True)),
            response("second", task(creation=True)),
            response("done", task("completed", result={"content": []})),
            response("regression", task()),
        ],
    )
    assert result.failure is not None
    assert result.failure.kind == "tasks.terminal_state_changed"


def test_failure_signature_ignores_action_request_and_task_identifiers():
    signatures = []
    for prefix in ("first", "different"):
        actions = [
            request(prefix),
            request(prefix + "-poll", "tasks/get", target=prefix),
        ]
        events = [
            response(prefix, task("completed", creation=True, task_id=prefix)),
            response(prefix + "-poll", task(task_id=prefix)),
        ]
        result = evaluate_tasks(actions, events)
        assert result.failure is not None
        signatures.append(result.failure.signature)
    assert signatures[0] == signatures[1]


def test_acknowledgement_metadata_must_be_an_object():
    result = evaluate_tasks(
        [request("cancel", "tasks/cancel")],
        [response("cancel", {"resultType": "complete", "_meta": []})],
    )
    assert result.failure is not None
    assert result.failure.kind == "tasks.invalid_acknowledgement"
