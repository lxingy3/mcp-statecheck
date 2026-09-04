import sys

import anyio
import httpx
import pytest

from mcp_statecheck.execution import ExecutionProtocolError
from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.task_execution import execute_task_actions
from mcp_statecheck.transports import StreamableHTTPTransport


def test_task_http_routing_uses_task_id():
    observed = []

    def receive(request):
        observed.append(dict(request.headers))
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 2, "result": {"resultType": "complete"}}
        )

    async def run():
        async with StreamableHTTPTransport(
            "http://127.0.0.1/mcp", transport=httpx.MockTransport(receive)
        ) as transport:
            for method in ("tasks/get", "tasks/update", "tasks/cancel"):
                await transport.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": method,
                        "params": {
                            "taskId": "server-task-73",
                            "_meta": {
                                "io.modelcontextprotocol/protocolVersion": "2026-07-28"
                            },
                        },
                    }
                )

    anyio.run(run)
    assert [headers.get("mcp-name") for headers in observed] == ["server-task-73"] * 3


def test_invalid_task_reference_is_rejected_before_launch():
    action = Action(
        "poll",
        ActionKind.REQUEST,
        mcp_request_id=2,
        method="tasks/get",
        target_action_id="missing",
        payload={},
        protocol_version="2026-07-28",
        capabilities={},
    )

    async def run():
        await execute_task_actions((action,), command=("must-not-be-launched",))

    with pytest.raises(ExecutionProtocolError, match="earlier tools/call"):
        anyio.run(run)


def test_task_reference_uses_the_handle_returned_by_the_peer():
    actions = (
        Action(
            "create",
            ActionKind.REQUEST,
            mcp_request_id=1,
            method="tools/call",
            payload={"name": "task_fixture", "arguments": {}},
            protocol_version="2026-07-28",
            capabilities={},
        ),
        Action(
            "poll",
            ActionKind.REQUEST,
            mcp_request_id=2,
            method="tasks/get",
            target_action_id="create",
            payload={},
            protocol_version="2026-07-28",
            capabilities={},
        ),
    )
    program = """
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if m['method'] == 'tools/call':
        value = {'resultType': 'task', 'taskId': 'server-minted-73'}
    else:
        assert m['params']['taskId'] == 'server-minted-73'
        value = {'resultType': 'complete', 'taskId': m['params']['taskId']}
    print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': value}), flush=True)
"""

    async def run():
        return await execute_task_actions(
            actions, command=(sys.executable, "-c", program)
        )

    result = anyio.run(run)
    assert result.returncode == 0
    assert result.events[1]["payload"]["taskId"] == "server-minted-73"
    assert result.cleanup["server_reaped"] is True
