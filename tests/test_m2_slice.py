from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.run_m2_slice as m2_script
from mcp_statecheck.execution import ExecutionResult
from mcp_statecheck.invariants import detect_failure
from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.stateful import ShrinkResult

CHECKED_IN_ARTIFACT = (
    Path(__file__).parents[1] / "artifacts" / "m2" / "request-before-initialized.json"
)


def test_request_before_initialized_slice_is_deterministic(tmp_path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    m2_script.build_artifact(first, seed=20_260_716, timeout=5)
    m2_script.build_artifact(second, seed=20_260_716, timeout=5)

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes() == CHECKED_IN_ARTIFACT.read_bytes()
    artifact = json.loads(first.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    assert artifact["fixture_id"] == "request-before-initialized"
    assert artifact["protocol_version"] == "2025-11-25"
    assert artifact["generation"]["engine"] == "Hypothesis RuleBasedStateMachine"
    assert artifact["cleanup"] == {
        "shrink_peer_reaped": True,
        "shrink_peer_returncode": 0,
    }
    assert [action["sequence"] for action in artifact["canonical_actions"]] == [
        1,
        2,
    ]
    assert [event["sequence"] for event in artifact["normalized_events"]] == [
        3,
        4,
    ]
    assert artifact["failure"]["kind"] == (
        "lifecycle.client_request_before_initialize_response"
    )
    assert [
        (action["kind"], action["method"])
        for action in artifact["failure"]["minimized_reproducer"]
    ] == [("initialize", None), ("request", "tools/list")]
    assert artifact["replay"]["attempts"] == 10
    assert artifact["replay"]["matched"] == 10
    assert artifact["replay"]["returncodes"] == [0] * 10


def test_replay_failure_preserves_existing_artifact(tmp_path, monkeypatch) -> None:
    output = tmp_path / "known-good.json"
    original = b'{"status":"known-good"}\n'
    output.write_bytes(original)
    actions = (
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
        ),
        Action(
            "tools-list",
            ActionKind.REQUEST,
            mcp_request_id=2,
            method="tools/list",
        ),
    )
    failure = detect_failure(actions, ())
    assert failure is not None
    generated = ShrinkResult(
        actions=actions,
        execution=ExecutionResult((), 0, ""),
        failure=failure,
        seed=20_260_716,
        hypothesis_version="test",
        settings={},
    )

    async def fail_replay(*_args) -> None:
        raise RuntimeError("controlled replay failure")

    monkeypatch.setattr(
        m2_script,
        "shrink_request_before_initialize",
        lambda *_args, **_kwargs: generated,
    )
    monkeypatch.setattr(m2_script, "_replay_saved", fail_replay)

    with pytest.raises(RuntimeError, match="controlled replay failure"):
        m2_script.build_artifact(output, seed=20_260_716, timeout=5)

    assert output.read_bytes() == original
