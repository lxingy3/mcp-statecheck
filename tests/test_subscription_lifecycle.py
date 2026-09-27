from mcp_statecheck.subscription_campaign import (
    FILTERS,
    SUBSCRIPTION_ID,
    action,
    evaluate,
)
from mcp_statecheck.subscription_lifecycle import _after_cancel


def test_open_subscription_and_late_delivery_have_distinct_results():
    first, second = action(1, FILTERS[0]), action(2, FILTERS[0])
    opened = [
        {
            "kind": "notification",
            "method": "notifications/subscriptions/acknowledged",
            "payload": {"_meta": {SUBSCRIPTION_ID: 1}, "notifications": FILTERS[0]},
        },
        {
            "kind": "notification",
            "method": "notifications/tools/list_changed",
            "payload": {"_meta": {SUBSCRIPTION_ID: 1}},
        },
    ]
    assert evaluate((first,), opened, require_close=False) is None
    assert evaluate((first,), opened).kind == "subscriptions.incomplete"
    late = [opened[1]]
    assert (
        _after_cancel(first, second, late).kind
        == "subscriptions.notification_after_cancel"
    )
