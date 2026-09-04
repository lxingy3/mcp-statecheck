"""Generated Tasks fault campaigns and versioned controlled replay."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio
from hypothesis.stateful import RuleBasedStateMachine, initialize, precondition, rule

from .execution import ExecutionProtocolError, ExecutionResult
from .model import Action, ActionKind, JsonValue
from .replay import (
    ReplayAttempt,
    ReplayInfrastructureError,
    ReplayMismatch,
    ReplayResult,
)
from .stateful import ShrinkResult, _Counterexample, _run_machine
from .task_execution import execute_task_actions, validate_task_actions
from .tasks import PROTOCOL_VERSION, TASK_CAPABILITIES, evaluate_tasks
from .trace import TraceRecorder

TASK_FIXTURES = {
    "task-terminal-regression": ("complete", 3),
    "task-input-key-reuse": ("input", 4),
    "task-result-shape": ("complete", 2),
}
TASK_TRANSPORTS = ("stdio", "streamable-http")
TASK_RECIPE_KIND = "controlled-tasks"


def task_action(
    action_id: str,
    method: str,
    request_id: int,
    *,
    target: str | None = None,
    payload: dict[str, JsonValue] | None = None,
) -> Action:
    return Action(
        action_id,
        ActionKind.REQUEST,
        mcp_request_id=request_id,
        method=method,
        target_action_id=target,
        payload=payload or {},
        protocol_version=PROTOCOL_VERSION,
        capabilities=TASK_CAPABILITIES,
    )


def task_creation(scenario: str = "complete") -> Action:
    return task_action(
        "create",
        "tools/call",
        1,
        payload={
            "name": "task_fixture",
            "arguments": {"scenario": scenario},
        },
    )


def task_baseline(scenario: str) -> tuple[Action, ...]:
    actions = [task_creation(scenario)]
    methods = ["tasks/get", "tasks/get", "tasks/get"]
    if scenario == "input":
        methods = ["tasks/get", "tasks/update", "tasks/get", "tasks/get", "tasks/get"]
    elif scenario == "cancel":
        methods = ["tasks/cancel", "tasks/get", "tasks/get", "tasks/get"]
    for number, method in enumerate(methods, 2):
        actions.append(
            task_action(
                f"step-{number}",
                method,
                number,
                target="create",
                payload=_input_response() if method == "tasks/update" else {},
            )
        )
    return tuple(actions)


def _input_response() -> dict[str, JsonValue]:
    return {
        "inputResponses": {
            "approval": {"action": "accept", "content": {"approved": True}}
        }
    }


async def execute_task_fixture(
    actions: Sequence[Action],
    fixture_id: str,
    transport: str,
    timeout: float = 5.0,
) -> ExecutionResult:
    validate_task_actions(actions)
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if (
        fixture_id not in {*TASK_FIXTURES, "conforming"}
        or transport not in TASK_TRANSPORTS
    ):
        raise ValueError("unsupported controlled Tasks fixture or transport")
    if transport == "stdio":
        result = await execute_task_actions(
            actions,
            command=(
                sys.executable,
                "-I",
                "-m",
                "mcp_statecheck._task_peer",
                "--mode",
                fixture_id,
            ),
            timeout=timeout,
        )
        if result.returncode != 0 or result.cleanup.get("server_reaped") is not True:
            raise ReplayInfrastructureError("Tasks peer did not exit cleanly")
    else:
        from ._task_peer import ControlledTaskHTTPPeer

        with ControlledTaskHTTPPeer(fixture_id) as peer:
            result = await execute_task_actions(actions, url=peer.url, timeout=timeout)
        result = replace(
            result, cleanup={**result.cleanup, "listener_closed": peer.closed}
        )
        if not peer.closed or result.cleanup.get("client_closed") is not True:
            raise ReplayInfrastructureError("Tasks HTTP cleanup was not confirmed")
    if any(event.get("kind") in {"http_error", "timeout"} for event in result.events):
        raise ReplayInfrastructureError("Tasks fixture transport did not complete")
    responses = [event for event in result.events if event.get("kind") == "response"]
    if [event.get("target_action_id") for event in responses] != [
        action.action_id for action in actions
    ] or any(event.get("outcome") != "success" for event in responses):
        raise ReplayInfrastructureError(
            "controlled Tasks plan did not complete every request successfully"
        )
    return result


class _TaskMachine(RuleBasedStateMachine):
    def __init__(self, fixture_id: str, transport: str, timeout: float):
        super().__init__()
        self.fixture_id, self.transport, self.timeout = fixture_id, transport, timeout
        self.scenario = TASK_FIXTURES[fixture_id][0]
        self.actions: list[Action] = []
        self.polled = False
        self.updated = False

    @initialize()
    def create(self):
        self.actions.append(task_creation(self.scenario))

    @rule()
    def poll(self):
        self._append("tasks/get")
        self.polled = True

    @precondition(
        lambda self: self.scenario == "input" and self.polled and not self.updated
    )
    @rule()
    def provide_input(self):
        self._append("tasks/update", _input_response())
        self.updated = True

    @rule()
    def unrelated_list(self):
        number = len(self.actions) + 1
        self.actions.append(task_action(f"step-{number}", "tools/list", number))

    def _append(self, method: str, payload=None):
        number = len(self.actions) + 1
        self.actions.append(
            task_action(
                f"step-{number}", method, number, target="create", payload=payload
            )
        )

    def teardown(self):
        if sys.exception() is not None or not self.polled:
            return
        execution = anyio.run(
            execute_task_fixture,
            tuple(self.actions),
            self.fixture_id,
            self.transport,
            self.timeout,
        )
        evaluation = evaluate_tasks(self.actions, execution.events)
        if evaluation.failure is not None:
            raise _Counterexample(self.actions, execution, evaluation.failure)


def shrink_task_failure(
    fixture_id: str,
    transport: str,
    *,
    seed: int = 20260904,
    timeout: float = 5.0,
) -> ShrinkResult:
    if fixture_id not in TASK_FIXTURES or transport not in TASK_TRANSPORTS:
        raise ValueError("unsupported Tasks fixture or transport")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    return _run_machine(
        lambda: _TaskMachine(fixture_id, transport, timeout),
        seed=seed,
        max_examples=30,
        stateful_step_count=10,
        no_failure_message="no Tasks failure was generated",
    )


def task_target_recipe(fixture_id: str) -> dict[str, object]:
    if fixture_id not in TASK_FIXTURES:
        raise ValueError("unsupported Tasks fixture")
    return {"version": 2, "kind": TASK_RECIPE_KIND, "fixture_id": fixture_id}


async def replay_task_artifact(artifact, attempts: int, timeout: float) -> ReplayResult:
    """Validate the v2 contract completely before starting a controlled peer."""
    recipe = artifact.get("target_recipe")
    if (
        not isinstance(recipe, dict)
        or set(recipe) != {"version", "kind", "fixture_id"}
        or type(recipe.get("version")) is not int
        or recipe["version"] != 2
        or recipe.get("kind") != TASK_RECIPE_KIND
        or not isinstance(recipe.get("fixture_id"), str)
        or recipe["fixture_id"] not in TASK_FIXTURES
    ):
        raise ReplayInfrastructureError("invalid controlled Tasks target recipe")
    fixture_id = recipe["fixture_id"]
    if (
        artifact.get("fixture_id") != fixture_id
        or artifact.get("protocol_version") != PROTOCOL_VERSION
        or artifact.get("adapter") != "tasks-wire"
        or artifact.get("sdk_version") != "none"
        or artifact.get("transport") not in TASK_TRANSPORTS
    ):
        raise ReplayInfrastructureError(
            "Tasks artifact metadata does not match target recipe"
        )
    if type(attempts) is not int or attempts <= 0:
        raise ValueError("attempts must be a positive integer")
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    failure = artifact.get("failure")
    if not isinstance(failure, dict):
        raise ReplayInfrastructureError("Tasks replay requires a failure")
    try:
        actions = tuple(
            Action.from_dict(item) for item in failure["minimized_reproducer"]
        )
        validate_task_actions(actions)
    except (KeyError, ValueError, TypeError, ExecutionProtocolError) as exc:
        raise ReplayInfrastructureError("invalid Tasks reproducer") from exc
    for action in actions:
        if action.capabilities != TASK_CAPABILITIES:
            raise ReplayInfrastructureError(
                "Tasks recipe capabilities do not match the profile"
            )
        if action.method == "tools/call" and (
            action.payload
            != {
                "name": "task_fixture",
                "arguments": {"scenario": TASK_FIXTURES[fixture_id][0]},
            }
        ):
            raise ReplayInfrastructureError(
                "Tasks recipe requires its controlled tool scenario"
            )
        if (action.method == "tools/list" and action.payload) or (
            action.method == "tasks/update" and action.payload != _input_response()
        ):
            raise ReplayInfrastructureError(
                "Tasks recipe contains unsupported parameters"
            )
    completed = []
    for number in range(1, attempts + 1):
        result = await execute_task_fixture(
            actions, fixture_id, artifact["transport"], timeout
        )
        observed = evaluate_tasks(actions, result.events).failure
        if observed is None or observed.signature != failure["signature"]:
            raise ReplayMismatch(
                f"Tasks replay {number} did not reproduce the expected signature"
            )
        completed.append(ReplayAttempt(number, result, observed))
    return ReplayResult(failure["signature"], tuple(completed))


def build_task_artifact(
    output: Path,
    *,
    fixture_id: str,
    transport: str,
    seed: int = 20260904,
    timeout: float = 5.0,
) -> Path:
    result = shrink_task_failure(fixture_id, transport, seed=seed, timeout=timeout)
    evaluation = evaluate_tasks(result.actions, result.execution.events)
    recorder = TraceRecorder(
        protocol_version=PROTOCOL_VERSION,
        adapter="tasks-wire",
        sdk_version="none",
        transport=transport,
        seed=seed,
        fixture_id=fixture_id,
        environment=os.environ,
        cleanup=result.execution.cleanup,
        target_recipe=task_target_recipe(fixture_id),
        generation={
            "engine": "Hypothesis RuleBasedStateMachine",
            "version": result.hypothesis_version,
            "settings": result.settings,
            "profile": "tasks",
            "task_count": evaluation.task_count,
            "transitions": list(evaluation.transitions),
        },
    )
    for action, event in zip(result.actions, result.execution.events, strict=True):
        recorder.record_action(action.to_dict())
        recorder.record_event(event)
    recorder.set_failure(
        kind=result.failure.kind,
        spec_reference=result.failure.spec_reference,
        signature=result.failure.signature,
        minimized_reproducer=[a.to_dict() for a in result.actions],
        trigger_action_id=result.failure.trigger_action_id,
        evidence=result.failure.evidence,
    )
    from .replay import replay_artifact

    with TemporaryDirectory(prefix="mcp-tasks-replay-") as temporary:
        staged = recorder.write(Path(temporary) / "failure.json")
        replay = anyio.run(replay_artifact, staged, 10, timeout)
    recorder.set_replay(
        attempts=10,
        matched=10,
        signature=replay.expected_signature,
        returncodes=[attempt.execution.returncode for attempt in replay.attempts],
        cleanups=[attempt.execution.cleanup for attempt in replay.attempts],
    )
    return recorder.write(output)
