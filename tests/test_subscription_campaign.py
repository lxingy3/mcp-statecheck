from dataclasses import replace

import anyio
import pytest

from mcp_statecheck.execution import ExecutionProtocolError
from mcp_statecheck.replay import ReplayInfrastructureError
from mcp_statecheck.subscription_campaign import (
    FILTERS,
    FIXTURES,
    SUBSCRIPTION_ID,
    action,
    evaluate,
    execute_fixture,
    replay_subscription_artifact,
)


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
@pytest.mark.parametrize(
    ("mode", "expected"),
    tuple(
        zip(
            FIXTURES,
            (
                "subscriptions.notification_before_ack",
                "subscriptions.unknown_id",
                "subscriptions.unrequested_notification",
            ),
            strict=True,
        )
    ),
)
def test_controlled_delivery_fault_is_observed_on_wire(transport, mode, expected):
    actions = (action(1, FILTERS[0]),)
    execution = anyio.run(execute_fixture, actions, mode, transport)
    failure = evaluate(actions, execution.events)
    assert failure is not None and failure.kind == expected
    assert (
        execution.cleanup[
            "server_reaped" if transport == "stdio" else "listener_closed"
        ]
        is True
    )


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
def test_healthy_multiple_subscriptions_are_independent(transport):
    actions = (action(1, FILTERS[0]), action(2, FILTERS[1]))
    execution = anyio.run(execute_fixture, actions, "conforming", transport)
    assert evaluate(actions, execution.events) is None
    notifications = [
        event for event in execution.events if event["kind"] == "notification"
    ]
    assert [event["payload"]["_meta"][SUBSCRIPTION_ID] for event in notifications] == [
        1,
        1,
        2,
        2,
    ]


def test_malformed_notification_id_and_filter_are_failures():
    actions = (action(1, FILTERS[1]),)
    event = {
        "kind": "notification",
        "method": "notifications/subscriptions/acknowledged",
        "payload": {"_meta": {SUBSCRIPTION_ID: []}, "notifications": {}},
    }
    assert evaluate(actions, [event]).kind == "subscriptions.unknown_id"
    event["payload"]["_meta"][SUBSCRIPTION_ID] = 1
    event["payload"]["notifications"] = {"resourceSubscriptions": [{}]}
    assert evaluate(actions, [event]).kind == "subscriptions.invalid_ack_filter"


def test_interleaving_is_ordered_per_subscription_id():
    actions = (action(1, FILTERS[0]), action(2, FILTERS[1]))

    def notification(request_id, method, **fields):
        return {
            "kind": "notification",
            "method": method,
            "payload": {"_meta": {SUBSCRIPTION_ID: request_id}, **fields},
        }

    events = [
        notification(
            2, "notifications/subscriptions/acknowledged", notifications=FILTERS[1]
        ),
        notification(
            2, "notifications/resources/updated", uri="file:///controlled/config.json"
        ),
        notification(
            1, "notifications/subscriptions/acknowledged", notifications=FILTERS[0]
        ),
        notification(1, "notifications/tools/list_changed"),
        *(
            {
                "kind": "response",
                "target_action_id": f"request-{request_id}",
                "outcome": "success",
                "payload": {
                    "resultType": "complete",
                    "_meta": {SUBSCRIPTION_ID: request_id},
                },
            }
            for request_id in (2, 1)
        ),
    ]
    assert evaluate(actions, events) is None
    assert (
        evaluate(actions, [events[3], *events]).kind
        == "subscriptions.notification_before_ack"
    )


def test_replay_rejects_modified_plan_before_execution():
    actions = (replace(action(1, FILTERS[0]), method="tools/call"),)
    with pytest.raises(ExecutionProtocolError):
        anyio.run(execute_fixture, actions, FIXTURES[0], "stdio")
    with pytest.raises(
        ReplayInfrastructureError, match="invalid controlled subscription recipe"
    ):
        anyio.run(
            replay_subscription_artifact,
            {
                "target_recipe": {
                    "kind": "controlled-subscriptions",
                    "version": 2,
                    "fixture_id": "unknown",
                }
            },
            1,
            5.0,
        )
