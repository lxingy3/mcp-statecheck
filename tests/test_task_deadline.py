"""A total plan timeout must not leave a notification-emitting child alive."""

import sys

import anyio
import pytest

import mcp_statecheck.task_execution as execution
from mcp_statecheck.task_campaign import task_creation
from mcp_statecheck.transports import StdioTransport


def test_total_tasks_deadline_reaps_notification_emitting_child(monkeypatch):
    transports = []
    fallback_cleanup = []

    class TrackedTransport(StdioTransport):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            transports.append(self)

        async def close(self):
            try:
                await super().close()
            except BaseException:
                # Keep a failing regression test from leaking its own child.
                # The assertion below requires production cleanup to suffice.
                process = self._process
                if process is not None and process.returncode is None:
                    fallback_cleanup.append(True)
                    with anyio.CancelScope(shield=True):
                        process.kill()
                        await process.wait()
                        await process.aclose()
                raise

    monkeypatch.setattr(execution, "StdioTransport", TrackedTransport)
    program = """
import json
import sys
import time

sys.stdin.readline()
for _ in range(200):
    print(json.dumps({
        "jsonrpc": "2.0", "method": "notifications/progress", "params": {}
    }), flush=True)
    time.sleep(0.01)
"""

    async def run():
        await execution.execute_task_actions(
            (task_creation(),),
            command=(sys.executable, "-c", program),
            timeout=0.2,
        )

    with pytest.raises(TimeoutError):
        anyio.run(run)
    assert len(transports) == 1
    assert transports[0].returncode is not None
    assert transports[0].pid is None
    assert not fallback_cleanup
