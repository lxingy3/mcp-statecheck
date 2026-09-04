from dataclasses import replace
from pathlib import Path

import anyio
import pytest

from mcp_statecheck.model import Action
from mcp_statecheck.replay import ReplayInfrastructureError, replay_artifact
from mcp_statecheck.reports import load_artifact
from mcp_statecheck.task_campaign import (
    TASK_FIXTURES,
    build_task_artifact,
    execute_task_fixture,
    task_baseline,
)
from mcp_statecheck.tasks import evaluate_tasks


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
@pytest.mark.parametrize("fixture_id", TASK_FIXTURES)
def test_task_failure_shrinks_and_replays_through_public_artifact(
    tmp_path: Path, transport, fixture_id
):
    output = tmp_path / "failure.json"
    build_task_artifact(output, fixture_id=fixture_id, transport=transport)
    artifact = load_artifact(output)
    assert (
        len(artifact["failure"]["minimized_reproducer"]) == TASK_FIXTURES[fixture_id][1]
    )
    assert artifact["target_recipe"]["version"] == 2
    assert artifact["replay"]["matched"] == 10
    replay = anyio.run(replay_artifact, output, 2, 5.0)
    assert len(replay.attempts) == 2
    actions = tuple(
        Action.from_dict(item) for item in artifact["failure"]["minimized_reproducer"]
    )
    for removed in range(1, len(actions)):
        candidate = actions[:removed] + actions[removed + 1 :]
        execution = anyio.run(execute_task_fixture, candidate, fixture_id, transport)
        assert evaluate_tasks(candidate, execution.events).failure is None


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
@pytest.mark.parametrize(
    "scenario", ["complete", "input", "cancel", "fail", "tool_error"]
)
def test_conforming_task_scenarios_have_no_false_positive(transport, scenario):
    actions = task_baseline(scenario)
    execution = anyio.run(execute_task_fixture, actions, "conforming", transport)
    evaluation = evaluate_tasks(actions, execution.events)
    assert evaluation.failure is None
    assert evaluation.task_count == 1
    assert len(execution.events) == len(actions)
    expected_status = {"cancel": "cancelled", "fail": "failed"}.get(
        scenario, "completed"
    )
    assert execution.events[-1]["payload"]["status"] == expected_status


def test_controlled_campaign_rejects_incomplete_task_plan():
    baseline = task_baseline("complete")[:2]
    actions = (replace(baseline[0], capabilities={}), baseline[1])
    with pytest.raises(ReplayInfrastructureError, match="every request"):
        anyio.run(execute_task_fixture, actions, "conforming", "stdio")
