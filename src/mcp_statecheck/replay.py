"""Deterministic replay of minimized protocol failures."""

from __future__ import annotations

import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from math import isfinite

from .execution import ExecutionResult, execute_stdio
from .fixtures import SSE_RESUME_FIXTURE_ID, FixtureDefinition, fixture_by_id
from .invariants import Failure, detect_failure
from .model import Action
from .reports import Artifact, load_artifact

type CandidateExecutor = Callable[
    [tuple[Action, ...], float],
    Awaitable[ExecutionResult],
]

REPLAY_ATTEMPTS = 10
TARGET_RECIPE_VERSION = 1
TARGET_RECIPE_KIND = "controlled-fixture"
_TARGET_RECIPE_FIELDS = {"fixture_id", "kind", "version"}
_PROTOCOL_VERSION = "2025-11-25"


class ReplayMismatch(AssertionError):
    """A replay did not reproduce the expected failure signature."""


class ReplayInfrastructureError(RuntimeError):
    """The replay peer did not complete normally."""


@dataclass(frozen=True, slots=True)
class ReplayAttempt:
    number: int
    execution: ExecutionResult
    failure: Failure


@dataclass(frozen=True, slots=True)
class ReplayResult:
    expected_signature: str
    attempts: tuple[ReplayAttempt, ...]


def controlled_target_recipe(fixture_id: str) -> dict[str, object]:
    """Return the only replay recipe accepted by the v1 artifact contract."""

    try:
        fixture_by_id(fixture_id)
    except KeyError as exc:
        raise ValueError(f"unsupported controlled fixture: {fixture_id}") from exc
    return {
        "fixture_id": fixture_id,
        "kind": TARGET_RECIPE_KIND,
        "version": TARGET_RECIPE_VERSION,
    }


def _target_fixture(artifact: Artifact) -> FixtureDefinition:
    recipe = artifact.get("target_recipe")
    if not isinstance(recipe, dict):
        raise ReplayInfrastructureError("artifact must contain a target_recipe object")
    if set(recipe) != _TARGET_RECIPE_FIELDS:
        raise ReplayInfrastructureError(
            "target_recipe fields must be exactly fixture_id, kind, and version"
        )
    version = recipe["version"]
    if type(version) is not int or version != TARGET_RECIPE_VERSION:
        raise ReplayInfrastructureError(
            f"unsupported target_recipe version; expected {TARGET_RECIPE_VERSION}"
        )
    if recipe["kind"] != TARGET_RECIPE_KIND:
        raise ReplayInfrastructureError(
            f"unsupported target_recipe kind; expected {TARGET_RECIPE_KIND}"
        )
    fixture_id = recipe["fixture_id"]
    if not isinstance(fixture_id, str):
        raise ReplayInfrastructureError(
            "target_recipe fixture_id must be a controlled fixture"
        )
    try:
        fixture = fixture_by_id(fixture_id)
    except KeyError as exc:
        raise ReplayInfrastructureError(
            "target_recipe fixture_id must be a controlled fixture"
        ) from exc

    expected_adapter = (
        "controlled-wire" if fixture.transport == "streamable-http" else "wire"
    )
    if artifact.get("fixture_id") != fixture.fixture_id:
        raise ReplayInfrastructureError(
            "target_recipe fixture_id does not match artifact fixture_id"
        )
    if artifact["transport"] != fixture.transport:
        raise ReplayInfrastructureError(
            "target_recipe fixture transport does not match artifact transport"
        )
    if artifact["adapter"] != expected_adapter:
        raise ReplayInfrastructureError(
            "target_recipe fixture adapter does not match artifact adapter"
        )
    if artifact["protocol_version"] != _PROTOCOL_VERSION:
        raise ReplayInfrastructureError(
            f"target_recipe version 1 requires protocol {_PROTOCOL_VERSION}"
        )
    return fixture


def _failure(artifact: Artifact) -> tuple[tuple[Action, ...], str]:
    failure = artifact.get("failure")
    if not isinstance(failure, dict):
        raise ReplayInfrastructureError("artifact must contain a failure object")
    reproducer = failure["minimized_reproducer"]
    signature = failure["signature"]
    try:
        actions = tuple(Action.from_dict(action) for action in reproducer)
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayInfrastructureError(
            "artifact failure contains invalid reproducer actions"
        ) from exc
    return actions, signature


async def replay_artifact(
    path: str | os.PathLike[str],
    attempts: int = REPLAY_ATTEMPTS,
    timeout: float = 5.0,
) -> ReplayResult:
    """Replay one allowlisted package-controlled failure artifact."""

    artifact = load_artifact(path)
    fixture = _target_fixture(artifact)
    actions, signature = _failure(artifact)
    if fixture.transport == "streamable-http":
        from ._controlled_peer import execute_controlled_http_fault

        async def execute(
            candidate: tuple[Action, ...],
            execution_timeout: float,
        ) -> ExecutionResult:
            try:
                return await execute_controlled_http_fault(
                    candidate,
                    fixture.fixture_id,
                    execution_timeout,
                )
            except RuntimeError as exc:
                raise ReplayInfrastructureError(str(exc)) from exc

        return await replay_http_failure(
            actions,
            execute,
            expected_signature=signature,
            fixture_id=fixture.fixture_id,
            attempts=attempts,
            timeout=timeout,
        )
    return await replay_stdio_failure(
        actions,
        (
            sys.executable,
            "-I",
            "-m",
            "mcp_statecheck._controlled_peer",
            "--stdio",
            "--mode",
            fixture.fixture_id,
        ),
        expected_signature=signature,
        fixture_id=fixture.fixture_id,
        attempts=attempts,
        timeout=timeout,
    )


async def replay_stdio_failure(
    actions: Sequence[Action],
    command: Sequence[str],
    *,
    expected_signature: str,
    fixture_id: str | None = None,
    attempts: int = REPLAY_ATTEMPTS,
    timeout: float = 5.0,
) -> ReplayResult:
    """Replay one canonical failure repeatedly over fresh stdio peers."""

    async def execute(
        canonical_actions: tuple[Action, ...],
        execution_timeout: float,
    ) -> ExecutionResult:
        return await execute_stdio(
            canonical_actions,
            command,
            timeout=execution_timeout,
        )

    return await _replay_failure(
        actions,
        execute,
        expected_signature=expected_signature,
        fixture_id=fixture_id,
        expected_returncode=0,
        required_cleanup=(),
        attempts=attempts,
        timeout=timeout,
    )


async def replay_http_failure(
    actions: Sequence[Action],
    executor: CandidateExecutor,
    *,
    expected_signature: str,
    fixture_id: str,
    attempts: int = REPLAY_ATTEMPTS,
    timeout: float = 5.0,
) -> ReplayResult:
    """Replay one canonical failure against fresh HTTP fixtures."""

    if not callable(executor):
        raise TypeError("executor must be callable")
    if not isinstance(fixture_id, str) or not fixture_id:
        raise ValueError("fixture_id must be a non-empty string")
    return await _replay_failure(
        actions,
        executor,
        expected_signature=expected_signature,
        fixture_id=fixture_id,
        expected_returncode=None,
        required_cleanup=(
            "client_closed",
            "listener_closed",
            *(("session_deleted",) if fixture_id == SSE_RESUME_FIXTURE_ID else ()),
        ),
        attempts=attempts,
        timeout=timeout,
    )


async def _replay_failure(
    actions: Sequence[Action],
    executor: CandidateExecutor,
    *,
    expected_signature: str,
    fixture_id: str | None,
    expected_returncode: int | None,
    required_cleanup: tuple[str, ...],
    attempts: int,
    timeout: float,
) -> ReplayResult:
    if not isinstance(expected_signature, str) or not expected_signature:
        raise ValueError("expected_signature must be a non-empty string")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts <= 0:
        raise ValueError("attempts must be a positive integer")
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")

    canonical_actions = tuple(actions)
    completed: list[ReplayAttempt] = []
    for number in range(1, attempts + 1):
        execution = await executor(
            canonical_actions,
            timeout,
        )
        if execution.returncode != expected_returncode:
            raise ReplayInfrastructureError(
                f"replay {number} returned process status {execution.returncode}; "
                f"expected {expected_returncode}"
            )
        for key in required_cleanup:
            if execution.cleanup.get(key) is not True:
                raise ReplayInfrastructureError(
                    f"replay {number} did not confirm {key}"
                )
        failure = detect_failure(
            canonical_actions,
            execution.events,
            fixture_id=fixture_id,
        )
        if failure is None:
            raise ReplayMismatch(f"replay {number} did not reproduce a failure")
        if failure.signature != expected_signature:
            raise ReplayMismatch(
                f"replay {number} produced a different failure signature"
            )
        completed.append(ReplayAttempt(number, execution, failure))

    return ReplayResult(expected_signature, tuple(completed))
