"""Deterministic replay of minimized protocol failures."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from math import isfinite

from .execution import ExecutionResult, execute_stdio
from .fixtures import SSE_RESUME_FIXTURE_ID
from .invariants import Failure, detect_failure
from .model import Action

type CandidateExecutor = Callable[
    [tuple[Action, ...], float],
    Awaitable[ExecutionResult],
]


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


async def replay_stdio_failure(
    actions: Sequence[Action],
    command: Sequence[str],
    *,
    expected_signature: str,
    fixture_id: str | None = None,
    attempts: int = 10,
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
    attempts: int = 10,
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
