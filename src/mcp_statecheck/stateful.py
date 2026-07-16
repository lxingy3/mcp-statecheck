"""Stateful generation and shrinking over real MCP transports."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import isfinite

import anyio
import hypothesis
from hypothesis import Phase, settings
from hypothesis import seed as hypothesis_seed
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    rule,
    run_state_machine_as_test,
)

from .execution import ExecutionResult, execute_stdio
from .invariants import Failure, detect_failure
from .model import Action, ActionKind, JsonValue


@dataclass(frozen=True, slots=True)
class ShrinkResult:
    """The minimized failure and the real execution that reproduced it."""

    actions: tuple[Action, ...]
    execution: ExecutionResult
    failure: Failure
    seed: int
    hypothesis_version: str
    settings: dict[str, JsonValue]


class NoFailureFound(RuntimeError):
    """Generation completed without finding the requested failure."""


class _Counterexample(AssertionError):
    def __init__(
        self,
        actions: Sequence[Action],
        execution: ExecutionResult,
        failure: Failure,
    ) -> None:
        self.actions = tuple(actions)
        self.execution = execution
        self.failure = failure
        super().__init__(failure.signature)


class _EarlyRequestMachine(RuleBasedStateMachine):
    def __init__(self, command: tuple[str, ...], timeout: float) -> None:
        super().__init__()
        self._command = command
        self._timeout = timeout
        self._next_id = 1
        self.actions: list[Action] = []

    @initialize()
    def initialize_connection(self) -> None:
        self.actions.append(
            Action(
                "initialize-pending",
                ActionKind.INITIALIZE,
                mcp_request_id=self._take_id(),
                protocol_version="2025-11-25",
                capabilities={},
            )
        )

    @rule()
    def ping(self) -> None:
        request_id = self._take_id()
        self.actions.append(
            Action(
                f"ping-{request_id}",
                ActionKind.REQUEST,
                mcp_request_id=request_id,
                method="ping",
                payload={},
            )
        )

    @rule()
    def tools_list(self) -> None:
        request_id = self._take_id()
        self.actions.append(
            Action(
                f"tools-list-{request_id}",
                ActionKind.REQUEST,
                mcp_request_id=request_id,
                method="tools/list",
                payload={},
            )
        )

    def teardown(self) -> None:
        if sys.exception() is not None or not any(
            action.kind is ActionKind.REQUEST and action.method != "ping"
            for action in self.actions
        ):
            return

        execution = anyio.run(
            _execute_candidate,
            tuple(self.actions),
            self._command,
            self._timeout,
        )
        failure = detect_failure(self.actions, execution.events)
        if failure is not None:
            raise _Counterexample(self.actions, execution, failure)

    def _take_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id


class _DuplicateRequestIdMachine(RuleBasedStateMachine):
    def __init__(self, command: tuple[str, ...], timeout: float) -> None:
        super().__init__()
        self._command = command
        self._timeout = timeout
        self._duplicate_added = False
        self._next_pair_id = 7
        self._next_ping_id = 100
        self.actions: list[Action] = []

    @initialize()
    def initialize_connection(self) -> None:
        self.actions.extend(
            (
                Action(
                    "initialize",
                    ActionKind.INITIALIZE,
                    mcp_request_id=1,
                    protocol_version="2025-11-25",
                    capabilities={},
                ),
                Action(
                    "initialize-response",
                    ActionKind.RESPONSE,
                    target_action_id="initialize",
                    protocol_version="2025-11-25",
                    capabilities={"tools": {}},
                ),
                Action("initialized", ActionKind.INITIALIZED),
            )
        )

    @rule()
    def duplicate_pair(self) -> None:
        request_id = self._next_pair_id
        self._next_pair_id += 1
        suffix = "" if request_id == 7 else f"-{request_id}"
        self._duplicate_added = True
        self.actions.extend(
            (
                Action(
                    f"call-a{suffix}",
                    ActionKind.REQUEST,
                    mcp_request_id=request_id,
                    method="tools/call",
                    payload={"label": "first"},
                ),
                Action(
                    f"call-b{suffix}",
                    ActionKind.REQUEST,
                    mcp_request_id=request_id,
                    method="tools/call",
                    payload={"label": "second"},
                ),
            )
        )

    @rule()
    def ping(self) -> None:
        request_id = self._next_ping_id
        self._next_ping_id += 1
        self.actions.append(
            Action(
                f"ping-{request_id}",
                ActionKind.REQUEST,
                mcp_request_id=request_id,
                method="ping",
                payload={},
            )
        )

    def teardown(self) -> None:
        if sys.exception() is not None or not self._duplicate_added:
            return
        execution = anyio.run(
            _execute_candidate,
            tuple(self.actions),
            self._command,
            self._timeout,
        )
        failure = detect_failure(self.actions, execution.events)
        if (
            failure is not None
            and failure.kind == "messages.request_id_reused_within_session"
        ):
            raise _Counterexample(self.actions, execution, failure)


async def _execute_candidate(
    actions: tuple[Action, ...], command: tuple[str, ...], timeout: float
) -> ExecutionResult:
    return await execute_stdio(actions, command, timeout=timeout)


def _validated_shrink_command(
    command: Sequence[str],
    *,
    seed: int,
    timeout: float,
    max_examples: int,
    stateful_step_count: int,
) -> tuple[str, ...]:
    normalized_command = tuple(command)
    if not normalized_command or not all(
        isinstance(part, str) and part for part in normalized_command
    ):
        raise ValueError("command must contain non-empty strings")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be an integer")
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if max_examples <= 0 or stateful_step_count <= 0:
        raise ValueError("Hypothesis limits must be positive")
    return normalized_command


def _run_machine(
    factory: Callable[[], RuleBasedStateMachine],
    *,
    seed: int,
    max_examples: int,
    stateful_step_count: int,
    no_failure_message: str,
) -> ShrinkResult:
    recorded_settings: dict[str, JsonValue] = {
        "database": None,
        "deadline": None,
        "max_examples": max_examples,
        "phases": ["generate", "shrink"],
        "report_multiple_bugs": False,
        "stateful_step_count": stateful_step_count,
    }
    seeded_factory = hypothesis_seed(seed)(factory)
    try:
        run_state_machine_as_test(
            seeded_factory,
            settings=settings(
                database=None,
                deadline=None,
                max_examples=max_examples,
                phases=(Phase.generate, Phase.shrink),
                report_multiple_bugs=False,
                stateful_step_count=stateful_step_count,
            ),
        )
    except _Counterexample as counterexample:
        return ShrinkResult(
            actions=counterexample.actions,
            execution=counterexample.execution,
            failure=counterexample.failure,
            seed=seed,
            hypothesis_version=hypothesis.__version__,
            settings=recorded_settings,
        )
    raise NoFailureFound(no_failure_message)


def shrink_request_before_initialize(
    command: Sequence[str],
    *,
    seed: int,
    timeout: float = 5.0,
    max_examples: int = 50,
    stateful_step_count: int = 8,
) -> ShrinkResult:
    """Find and shrink a client request sent before initialize completes."""

    normalized_command = _validated_shrink_command(
        command,
        seed=seed,
        timeout=timeout,
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
    )

    def factory() -> _EarlyRequestMachine:
        return _EarlyRequestMachine(normalized_command, timeout)

    return _run_machine(
        factory,
        seed=seed,
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
        no_failure_message="no request-before-initialize failure was generated",
    )


def shrink_duplicate_request_id(
    command: Sequence[str],
    *,
    seed: int,
    timeout: float = 5.0,
    max_examples: int = 50,
    stateful_step_count: int = 8,
) -> ShrinkResult:
    """Find and shrink a request ID reused within one session."""

    normalized_command = _validated_shrink_command(
        command,
        seed=seed,
        timeout=timeout,
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
    )

    def factory() -> _DuplicateRequestIdMachine:
        return _DuplicateRequestIdMachine(normalized_command, timeout)

    return _run_machine(
        factory,
        seed=seed,
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
        no_failure_message="no duplicate request ID failure was generated",
    )
