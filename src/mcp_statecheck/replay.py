"""Deterministic replay of minimized protocol failures."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

from .execution import ExecutionResult, execute_stdio
from .invariants import Failure, detect_failure
from .model import Action


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
    attempts: int = 10,
    timeout: float = 5.0,
) -> ReplayResult:
    """Replay one canonical failure repeatedly over fresh stdio peers."""

    if not isinstance(expected_signature, str) or not expected_signature:
        raise ValueError("expected_signature must be a non-empty string")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts <= 0:
        raise ValueError("attempts must be a positive integer")
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")

    canonical_actions = tuple(actions)
    completed: list[ReplayAttempt] = []
    for number in range(1, attempts + 1):
        execution = await execute_stdio(
            canonical_actions,
            command,
            timeout=timeout,
        )
        if execution.returncode != 0:
            raise ReplayInfrastructureError(
                f"replay {number} peer exited with status {execution.returncode}"
            )
        failure = detect_failure(canonical_actions, execution.events)
        if failure is None:
            raise ReplayMismatch(f"replay {number} did not reproduce a failure")
        if failure.signature != expected_signature:
            raise ReplayMismatch(
                f"replay {number} produced a different failure signature"
            )
        completed.append(ReplayAttempt(number, execution, failure))

    return ReplayResult(expected_signature, tuple(completed))
