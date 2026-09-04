"""Malformed Tasks replay inputs never reach a controlled peer."""

import json

import anyio
import pytest

import mcp_statecheck.task_campaign as campaign
from mcp_statecheck.replay import ReplayInfrastructureError, replay_artifact
from mcp_statecheck.task_campaign import task_baseline, task_target_recipe
from mcp_statecheck.trace import TraceRecorder


def _artifact():
    actions = task_baseline("complete")[:3]
    recorder = TraceRecorder(
        protocol_version="2026-07-28",
        adapter="tasks-wire",
        sdk_version="none",
        transport="streamable-http",
        seed=20260904,
        fixture_id="task-terminal-regression",
        target_recipe=task_target_recipe("task-terminal-regression"),
        environment={},
    )
    for action in actions:
        recorder.record_action(action.to_dict())
    recorder.set_failure(
        kind="tasks.terminal_state_changed",
        spec_reference="https://tasks.extensions.modelcontextprotocol.io/",
        signature="validation-only",
        minimized_reproducer=[action.to_dict() for action in actions],
    )
    return recorder.artifact()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("target_recipe", "version"), True),
        (("target_recipe", "version"), 2.0),
        (("target_recipe", "version"), 3),
        (("target_recipe", "kind"), "external-command"),
        (("target_recipe", "fixture_id"), "unknown"),
        (("target_recipe", "command"), ["must-not-be-launched"]),
        (("fixture_id",), "task-result-shape"),
        (("protocol_version",), "2025-11-25"),
        (("adapter",), "wire"),
        (("sdk_version",), "2.1.1"),
        (("transport",), "external"),
        (("failure", "minimized_reproducer", 0, "capabilities"), {}),
        (("failure", "minimized_reproducer", 0, "payload"), {"name": "external"}),
        (("failure", "minimized_reproducer", 1, "target_action_id"), "future"),
        (("failure", "minimized_reproducer", 1, "mcp_request_id"), 1),
        (("failure", "minimized_reproducer", 1, "mcp_request_id"), True),
        (("failure", "minimized_reproducer", 1, "payload"), {"taskId": "guessed"}),
        (("failure", "minimized_reproducer", 1, "payload"), {"unrecognized": True}),
        (("failure", "minimized_reproducer", 1, "method"), "tasks/update"),
        (("failure", "minimized_reproducer", 1, "stream_id"), "unexpected-stream"),
    ],
)
def test_tampered_tasks_recipe_is_rejected_before_execution(
    tmp_path, monkeypatch, path, value
):
    artifact = _artifact()
    destination = artifact
    for key in path[:-1]:
        destination = destination[key]
    destination[path[-1]] = value
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(artifact), encoding="utf-8")

    async def forbid_execution(*args, **kwargs):
        pytest.fail("malformed artifact reached controlled peer execution")

    monkeypatch.setattr(campaign, "execute_task_fixture", forbid_execution)
    with pytest.raises(ReplayInfrastructureError):
        anyio.run(replay_artifact, source)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected_before_replay_execution(
    tmp_path, monkeypatch, timeout
):
    source = tmp_path / "failure.json"
    source.write_text(json.dumps(_artifact()), encoding="utf-8")

    async def forbid_execution(*args, **kwargs):
        pytest.fail("invalid timeout reached controlled peer execution")

    monkeypatch.setattr(campaign, "execute_task_fixture", forbid_execution)
    with pytest.raises(ValueError, match="finite and positive"):
        anyio.run(replay_artifact, source, 1, timeout)


@pytest.mark.parametrize("attempts", [0, -1, True, 1.5])
def test_invalid_attempt_count_is_rejected_before_replay_execution(
    tmp_path, monkeypatch, attempts
):
    source = tmp_path / "failure.json"
    source.write_text(json.dumps(_artifact()), encoding="utf-8")

    async def forbid_execution(*args, **kwargs):
        pytest.fail("invalid attempt count reached controlled peer execution")

    monkeypatch.setattr(campaign, "execute_task_fixture", forbid_execution)
    with pytest.raises(ValueError, match="positive integer"):
        anyio.run(replay_artifact, source, attempts, 5.0)
