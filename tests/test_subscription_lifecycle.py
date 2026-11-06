import sys

import anyio
import pytest

from mcp_statecheck._subscription_peer import ControlledSubscriptionHTTPPeer
from mcp_statecheck.execution import _wire_message, execute_http
from mcp_statecheck.subscription_campaign import (
    FILTERS,
    SUBSCRIPTION_ID,
    action,
    evaluate,
)
from mcp_statecheck.subscription_lifecycle import _after_cancel
from mcp_statecheck.transports import StdioTransport, StreamableHTTPTransport


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


@pytest.mark.parametrize("request_id", [37, "37", "listen:0"])
def test_stdio_cancel_matches_the_actual_subscription_and_id_type(request_id):
    async def run():
        command = (
            sys.executable,
            "-I",
            "-m",
            "mcp_statecheck._subscription_peer",
            "--mode",
            "held-cancel",
        )
        async with StdioTransport(command, timeout=5) as transport:
            first = _wire_message(action(request_id, FILTERS[0]))
            discover = {**first, "id": "discover", "method": "server/discover"}
            await transport.send(discover)
            assert "result" in await transport.receive()
            await transport.send(first)
            assert (await transport.receive())[
                "method"
            ] == "notifications/subscriptions/acknowledged"
            assert (await transport.receive())[
                "method"
            ] == "notifications/tools/list_changed"

            async def cancel(value):
                await transport.send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": value},
                    }
                )

            await cancel(str(request_id) if type(request_id) is int else 37)
            await transport.send(_wire_message(action("too-early", FILTERS[0])))
            assert "error" in await transport.receive()
            await cancel(request_id)
            await cancel("unrelated")
            await transport.send(_wire_message(action("replacement", FILTERS[0])))
            for method in (
                "notifications/subscriptions/acknowledged",
                "notifications/tools/list_changed",
            ):
                assert (await transport.receive())["method"] == method
            result = await transport.receive()
            assert result["id"] == "replacement" and "result" in result
        assert transport.returncode == 0

    anyio.run(run)


def test_http_drop_recovery_can_reuse_a_new_clients_request_id():
    async def run():
        actions = (action("listen:0", FILTERS[0]),)
        with ControlledSubscriptionHTTPPeer("drop-first") as peer:
            async with StreamableHTTPTransport(peer.url, timeout=5) as transport:
                first = await transport.send(_wire_message(actions[0]))
            assert [message.get("method") for message in first] == [
                "notifications/subscriptions/acknowledged",
                "notifications/tools/list_changed",
            ]
            replacement = await execute_http(actions, peer.url, timeout=5)
            assert len(replacement.events) == 3
            assert evaluate(actions, replacement.events) is None
        assert peer.closed

    anyio.run(run)
