import pytest

from mcp_statecheck._subscription_peer import _ConcurrentSubscriptions
from mcp_statecheck.execution import _wire_message
from mcp_statecheck.subscription_campaign import FILTERS, SUBSCRIPTION_ID, action


@pytest.mark.parametrize("cancelled_index", [0, 1])
@pytest.mark.parametrize("cancel_before_barrier", [False, True])
def test_cancel_preserves_the_other_live_stream(cancelled_index, cancel_before_barrier):
    peer = _ConcurrentSubscriptions()
    emitted = []
    ids = [37, "37"]
    for request_id, requested in zip(ids, FILTERS[:2], strict=True):
        peer.handle(_wire_message(action(request_id, requested)), emitted.append)
    assert [frame["method"] for frame in emitted] == [
        "notifications/subscriptions/acknowledged",
        "notifications/subscriptions/acknowledged",
    ]
    emitted.clear()
    peer.handle(_wire_message(action(100, method="tools/list")), emitted.append)
    assert [frame["params"]["_meta"][SUBSCRIPTION_ID] for frame in emitted[:2]] == [
        "37",
        37,
    ]
    assert emitted[-1]["id"] == 100
    emitted.clear()

    def cancel(request_id):
        peer.handle(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": request_id},
            },
            emitted.append,
        )

    for invalid in (None, [], True, "unknown"):
        cancel(invalid)
    barrier = _wire_message(action(101, method="tools/list"))
    if cancel_before_barrier:
        cancel(ids[cancelled_index])
        assert emitted == []
        peer.handle(barrier, emitted.append)
    else:
        peer.handle(barrier, emitted.append)
        assert emitted == []
        cancel(ids[cancelled_index])

    survivor = ids[1 - cancelled_index]
    assert len(emitted) == 3
    notice, completion, response = emitted
    assert notice["params"]["_meta"][SUBSCRIPTION_ID] == survivor
    assert type(notice["params"]["_meta"][SUBSCRIPTION_ID]) is type(survivor)
    assert (
        completion["id"] == survivor
        and completion["result"]["resultType"] == "complete"
    )
    assert response["id"] == 101 and response["result"]["tools"] == []


def test_invalid_listen_does_not_consume_a_slot_and_duplicate_ids_fail():
    peer = _ConcurrentSubscriptions()
    emitted = []
    invalid = _wire_message(action(1, FILTERS[0]))
    invalid["params"]["notifications"] = {"toolsListChanged": "yes"}
    peer.handle(invalid, emitted.append)
    assert "error" in emitted.pop()
    first = _wire_message(action(1, FILTERS[0]))
    peer.handle(first, emitted.append)
    assert emitted.pop()["method"] == "notifications/subscriptions/acknowledged"
    peer.handle(first, emitted.append)
    assert "error" in emitted.pop()
    peer.handle(_wire_message(action(2, FILTERS[1])), emitted.append)
    assert emitted.pop()["method"] == "notifications/subscriptions/acknowledged"
    peer.handle(_wire_message(action(3, FILTERS[0])), emitted.append)
    assert "error" in emitted.pop()


def test_cancelling_both_streams_cannot_pass_the_delivery_barrier():
    peer = _ConcurrentSubscriptions()
    emitted = []
    for request_id, requested in enumerate(FILTERS[:2]):
        peer.handle(_wire_message(action(request_id, requested)), emitted.append)
    peer.handle(_wire_message(action(100, method="tools/list")), emitted.append)
    emitted.clear()
    peer.cancel(0)
    peer.cancel(1)
    peer.handle(_wire_message(action(101, method="tools/list")), emitted.append)
    assert len(emitted) == 1 and "error" in emitted[0]
