"""Acceptance validation tests without repeating generated campaigns."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from mcp_statecheck._task_peer import TaskPeerState
from mcp_statecheck.task_campaign import TASK_FIXTURES, task_baseline
from mcp_statecheck.tasks import TASK_CAPABILITIES, evaluate_tasks
from scripts.run_m6_tasks_acceptance import (
    AcceptanceError,
    _compare_payloads,
    _require_exact_files,
    _validate_baseline,
    _validate_trace,
)


def trace(
    fixture: str = "task-terminal-regression", transport: str = "stdio"
) -> dict[str, Any]:
    scenario, minimum = TASK_FIXTURES[fixture]
    actions = task_baseline(scenario)[:minimum]
    peer = TaskPeerState(fixture)
    events = []
    for action in actions:
        params = {
            **action.payload,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": TASK_CAPABILITIES,
                "io.modelcontextprotocol/clientInfo": {
                    "name": "acceptance-test",
                    "version": "1",
                },
            },
        }
        if action.target_action_id:
            params["taskId"] = "task-1"
        response = peer.handle(
            {
                "jsonrpc": "2.0",
                "id": action.mcp_request_id,
                "method": action.method,
                "params": params,
            }
        )
        events.append(
            {
                "kind": "response",
                "target_action_id": action.action_id,
                "mcp_request_id": action.mcp_request_id,
                "outcome": "success",
                "payload": response["result"],
            }
        )
    evaluated = evaluate_tasks(actions, events)
    assert evaluated.failure is not None
    cleanup = (
        {"server_reaped": True}
        if transport == "stdio"
        else {"client_closed": True, "listener_closed": True}
    )
    return {
        "schema_version": 1,
        "adapter": "tasks-wire",
        "sdk_version": "none",
        "protocol_version": "2026-07-28",
        "seed": 20260904,
        "fixture_id": fixture,
        "transport": transport,
        "target_recipe": {
            "version": 2,
            "kind": "controlled-tasks",
            "fixture_id": fixture,
        },
        "canonical_actions": [action.to_dict() for action in actions],
        "normalized_events": events,
        "cleanup": cleanup,
        "generation": {
            "profile": "tasks",
            "engine": "Hypothesis RuleBasedStateMachine",
            "transitions": list(evaluated.transitions),
            "task_count": 1,
        },
        "failure": {
            "kind": evaluated.failure.kind,
            "signature": evaluated.failure.signature,
            "minimized_reproducer": [action.to_dict() for action in actions],
        },
        "replay": {
            "attempts": 10,
            "matched": 10,
            "signature": evaluated.failure.signature,
            "returncodes": [0 if transport == "stdio" else None] * 10,
            "cleanups": [deepcopy(cleanup) for _ in range(10)],
        },
    }


def test_exact_artifact_set_rejects_missing_and_extra_files(tmp_path: Path) -> None:
    expected = {
        Path("acceptance.json"),
        *(
            Path(transport) / f"{fixture}.json"
            for transport in ("stdio", "streamable-http")
            for fixture in (
                "task-terminal-regression",
                "task-input-key-reuse",
                "task-result-shape",
            )
        ),
    }
    for relative in expected:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    _require_exact_files(tmp_path)
    (tmp_path / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(AcceptanceError, match="exactly"):
        _require_exact_files(tmp_path)
    (tmp_path / "extra.txt").unlink()
    (tmp_path / "acceptance.json").unlink()
    with pytest.raises(AcceptanceError, match="exactly"):
        _require_exact_files(tmp_path)


@pytest.mark.parametrize("fixture", TASK_FIXTURES)
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
def test_trace_validation_recomputes_failure_and_checks_cleanup(
    fixture: str, transport: str
) -> None:
    value = trace(fixture, transport)
    row = _validate_trace(value, fixture_id=fixture, transport=transport)
    assert row["minimized_action_count"] == TASK_FIXTURES[fixture][1]
    assert row["replay_matched"] == 10
    value["replay"]["cleanups"][3] = {}
    with pytest.raises(AcceptanceError, match="cleanup"):
        _validate_trace(value, fixture_id=fixture, transport=transport)


@pytest.mark.parametrize(
    "mutation",
    [
        "signature",
        "event",
        "minimum",
        "replay",
        "metadata",
        "boolean-schema",
        "boolean-returncode",
    ],
)
def test_trace_validator_rejects_inconsistent_evidence(mutation: str) -> None:
    value = trace()
    if mutation == "signature":
        value["failure"]["signature"] = "forged"
    elif mutation == "event":
        value["normalized_events"][-1]["payload"]["status"] = "completed"
    elif mutation == "minimum":
        value["failure"]["minimized_reproducer"].pop()
    elif mutation == "replay":
        value["replay"]["matched"] = 9
    elif mutation == "boolean-schema":
        value["schema_version"] = True
    elif mutation == "boolean-returncode":
        value["replay"]["returncodes"][0] = False
    else:
        value["adapter"] = "not-tasks-wire"
    with pytest.raises(AcceptanceError):
        _validate_trace(value, fixture_id="task-terminal-regression", transport="stdio")


def test_payload_comparison_rejects_byte_mismatch_and_missing_trace() -> None:
    first = {Path("trace.json"): b"first"}
    _compare_payloads(first, dict(first), label="fresh run")
    with pytest.raises(AcceptanceError, match="byte-identical"):
        _compare_payloads(first, {Path("trace.json"): b"changed"}, label="fresh run")
    with pytest.raises(AcceptanceError, match="file set"):
        _compare_payloads(first, {}, label="fresh run")


def test_healthy_baseline_requires_observed_terminal_status() -> None:
    actions = task_baseline("complete")
    peer = TaskPeerState()
    events = []
    for action in actions:
        params = {
            **action.payload,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": TASK_CAPABILITIES,
                "io.modelcontextprotocol/clientInfo": {
                    "name": "acceptance-test",
                    "version": "1",
                },
            },
        }
        if action.target_action_id:
            params["taskId"] = "task-1"
        response = peer.handle(
            {
                "jsonrpc": "2.0",
                "id": action.mcp_request_id,
                "method": action.method,
                "params": params,
            }
        )
        events.append(
            {
                "kind": "response",
                "target_action_id": action.action_id,
                "mcp_request_id": action.mcp_request_id,
                "outcome": "success",
                "payload": response["result"],
            }
        )
    row = _validate_baseline(
        actions,
        events,
        scenario="complete",
        transport="stdio",
        cleanup={"server_reaped": True},
        returncode=0,
    )
    assert row["terminal_status"] == "completed"
    assert row["transitions"] == ["created->working", "working->completed"]
    errors = deepcopy(events)
    for event in errors[1:]:
        event["outcome"] = "error"
        event["payload"] = {"code": -32603, "message": "controlled failure"}
    with pytest.raises(AcceptanceError, match="wire responses"):
        _validate_baseline(
            actions,
            errors,
            scenario="complete",
            transport="stdio",
            cleanup={"server_reaped": True},
            returncode=0,
        )
    wrong_result = deepcopy(events)
    wrong_result[-1]["payload"]["result"]["isError"] = True
    with pytest.raises(AcceptanceError, match="payload"):
        _validate_baseline(
            actions,
            wrong_result,
            scenario="complete",
            transport="stdio",
            cleanup={"server_reaped": True},
            returncode=0,
        )
    for event in events[1:]:
        event["payload"]["status"] = "working"
        event["payload"].pop("result")
    with pytest.raises(AcceptanceError, match="terminal"):
        _validate_baseline(
            actions,
            events,
            scenario="complete",
            transport="stdio",
            cleanup={"server_reaped": True},
            returncode=0,
        )
