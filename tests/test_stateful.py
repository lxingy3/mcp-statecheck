from __future__ import annotations

import sys
from pathlib import Path

from mcp_statecheck.model import ActionKind
from mcp_statecheck.stateful import shrink_request_before_initialize

PEER = Path(__file__).parent / "fixtures" / "peer.py"


def test_real_stdio_failure_shrinks_to_initialize_then_tools_list() -> None:
    result = shrink_request_before_initialize(
        (
            sys.executable,
            str(PEER),
            "--stdio",
            "--mode",
            "request-before-initialized",
        ),
        seed=20_260_716,
        timeout=5,
    )

    assert tuple((action.kind, action.method) for action in result.actions) == (
        (ActionKind.INITIALIZE, None),
        (ActionKind.REQUEST, "tools/list"),
    )
    assert result.failure.trigger_action_id == result.actions[-1].action_id
    assert result.execution.returncode == 0
    assert result.execution.stderr == ""
    assert tuple(event["target_action_id"] for event in result.execution.events) == (
        result.actions[-1].action_id,
        result.actions[0].action_id,
    )
