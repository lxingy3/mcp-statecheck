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
from .fixtures import HTTP_ERROR_FIXTURE_ID, SSE_RESUME_FIXTURE_ID
from .invariants import (
    HTTP_STATUS_TIMEOUT_KIND,
    LATE_RESPONSE_FIXTURE_ID,
    SSE_RESUME_TOKEN_LOST_KIND,
    WRONG_RESPONSE_CORRELATION_KIND,
    Failure,
    detect_failure,
)
from .model import Action, ActionKind, JsonValue
from .replay import CandidateExecutor


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


def _initialized_actions() -> tuple[Action, ...]:
    return (
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
        self.actions.extend(_initialized_actions())

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


class _LateResponseCorrelationMachine(RuleBasedStateMachine):
    def __init__(self, command: tuple[str, ...], timeout: float) -> None:
        super().__init__()
        self._command = command
        self._timeout = timeout
        self._pair_count = 0
        self.actions: list[Action] = []

    @initialize()
    def initialize_connection(self) -> None:
        self.actions.extend(_initialized_actions())

    @rule()
    def corrupted_pair(self) -> None:
        pair_number = self._pair_count + 1
        suffix = "" if pair_number == 1 else f"-{pair_number}"
        first_id = 21 + self._pair_count * 2
        second_id = first_id + 1
        first_action_id = f"call-a{suffix}"
        second_action_id = f"call-b{suffix}"
        self._pair_count = pair_number
        self.actions.extend(
            (
                Action(
                    first_action_id,
                    ActionKind.REQUEST,
                    mcp_request_id=first_id,
                    method="tools/call",
                    payload={
                        "arguments": {"fixtureCanary": first_action_id},
                        "name": "fixture",
                    },
                ),
                Action(
                    f"cancel-a{suffix}",
                    ActionKind.CANCEL,
                    mcp_request_id=first_id,
                    target_action_id=first_action_id,
                ),
                Action(
                    second_action_id,
                    ActionKind.REQUEST,
                    mcp_request_id=second_id,
                    method="tools/call",
                    payload={
                        "arguments": {"fixtureCanary": second_action_id},
                        "name": "fixture",
                    },
                ),
                Action(
                    f"misattributed-late-response{suffix}",
                    ActionKind.RESPONSE,
                    target_action_id=second_action_id,
                ),
                Action(
                    f"misattributed-current-response{suffix}",
                    ActionKind.RESPONSE,
                    target_action_id=first_action_id,
                ),
            )
        )

    def teardown(self) -> None:
        if sys.exception() is not None or self._pair_count == 0:
            return
        execution = anyio.run(
            _execute_candidate,
            tuple(self.actions),
            self._command,
            self._timeout,
        )
        failure = detect_failure(
            self.actions,
            execution.events,
            fixture_id=LATE_RESPONSE_FIXTURE_ID,
        )
        if failure is not None and failure.kind == WRONG_RESPONSE_CORRELATION_KIND:
            raise _Counterexample(self.actions, execution, failure)


class _HTTPErrorAsTimeoutMachine(RuleBasedStateMachine):
    def __init__(self, executor: CandidateExecutor, timeout: float) -> None:
        super().__init__()
        self._executor = executor
        self._timeout = timeout
        self._initialize_count = 0
        self.actions: list[Action] = []

    @initialize()
    def connect(self) -> None:
        self.actions.append(Action("connect", ActionKind.CONNECT))

    @rule()
    def initialize_request(self) -> None:
        self._initialize_count += 1
        suffix = "" if self._initialize_count == 1 else f"-{self._initialize_count}"
        self.actions.append(
            Action(
                f"initialize{suffix}",
                ActionKind.INITIALIZE,
                mcp_request_id=self._initialize_count,
                protocol_version="2025-11-25",
                capabilities={},
            )
        )

    def teardown(self) -> None:
        if sys.exception() is not None or self._initialize_count == 0:
            return
        execution = anyio.run(
            self._executor,
            tuple(self.actions),
            self._timeout,
        )
        failure = detect_failure(
            self.actions,
            execution.events,
            fixture_id=HTTP_ERROR_FIXTURE_ID,
        )
        if failure is not None and failure.kind == HTTP_STATUS_TIMEOUT_KIND:
            raise _Counterexample(self.actions, execution, failure)


class _SecondSSEResumeTokenLossMachine(RuleBasedStateMachine):
    def __init__(self, executor: CandidateExecutor, timeout: float) -> None:
        super().__init__()
        self._executor = executor
        self._timeout = timeout
        self._stream_added = False
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
                Action("initialized", ActionKind.INITIALIZED),
            )
        )

    @rule()
    def second_resume(self) -> None:
        if self._stream_added:
            return
        self._stream_added = True
        self.actions.extend(
            (
                Action(
                    "open-sse",
                    ActionKind.OPEN_STREAM,
                    stream_id="server-events",
                ),
                Action(
                    "resume-1",
                    ActionKind.RESUME_STREAM,
                    stream_id="server-events",
                    resume_token="cursor-1",
                ),
                Action(
                    "resume-2",
                    ActionKind.RESUME_STREAM,
                    stream_id="server-events",
                    resume_token="cursor-2",
                ),
            )
        )

    def teardown(self) -> None:
        if sys.exception() is not None or not self._stream_added:
            return
        execution = anyio.run(
            self._executor,
            tuple(self.actions),
            self._timeout,
        )
        failure = detect_failure(
            self.actions,
            execution.events,
            fixture_id=SSE_RESUME_FIXTURE_ID,
        )
        if failure is not None and failure.kind == SSE_RESUME_TOKEN_LOST_KIND:
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
    _validate_shrink_options(
        seed=seed,
        timeout=timeout,
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
    )
    normalized_command = tuple(command)
    if not normalized_command or not all(
        isinstance(part, str) and part for part in normalized_command
    ):
        raise ValueError("command must contain non-empty strings")
    return normalized_command


def _validated_candidate_executor(
    executor: CandidateExecutor,
    *,
    seed: int,
    timeout: float,
    max_examples: int,
    stateful_step_count: int,
) -> CandidateExecutor:
    _validate_shrink_options(
        seed=seed,
        timeout=timeout,
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
    )
    if not callable(executor):
        raise TypeError("executor must be callable")
    return executor


def _validate_shrink_options(
    *,
    seed: int,
    timeout: float,
    max_examples: int,
    stateful_step_count: int,
) -> None:
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be an integer")
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if max_examples <= 0 or stateful_step_count <= 0:
        raise ValueError("Hypothesis limits must be positive")


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


def shrink_late_response_correlation(
    command: Sequence[str],
    *,
    seed: int,
    timeout: float = 5.0,
    max_examples: int = 50,
    stateful_step_count: int = 8,
) -> ShrinkResult:
    """Find and shrink a late response correlated to a later request."""

    normalized_command = _validated_shrink_command(
        command,
        seed=seed,
        timeout=timeout,
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
    )

    def factory() -> _LateResponseCorrelationMachine:
        return _LateResponseCorrelationMachine(normalized_command, timeout)

    return _run_machine(
        factory,
        seed=seed,
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
        no_failure_message="no late-response correlation failure was generated",
    )


def shrink_http_error_as_timeout(
    executor: CandidateExecutor,
    *,
    seed: int,
    timeout: float = 5.0,
    max_examples: int = 50,
    stateful_step_count: int = 8,
) -> ShrinkResult:
    """Find and shrink an HTTP error misclassified as a timeout."""

    validated_executor = _validated_candidate_executor(
        executor,
        seed=seed,
        timeout=timeout,
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
    )

    def factory() -> _HTTPErrorAsTimeoutMachine:
        return _HTTPErrorAsTimeoutMachine(validated_executor, timeout)

    return _run_machine(
        factory,
        seed=seed,
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
        no_failure_message="no HTTP error-as-timeout failure was generated",
    )


def shrink_second_sse_resume_token_loss(
    executor: CandidateExecutor,
    *,
    seed: int,
    timeout: float = 5.0,
    max_examples: int = 50,
    stateful_step_count: int = 8,
) -> ShrinkResult:
    """Find and shrink a second SSE resume that loses the latest cursor."""

    validated_executor = _validated_candidate_executor(
        executor,
        seed=seed,
        timeout=timeout,
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
    )

    def factory() -> _SecondSSEResumeTokenLossMachine:
        return _SecondSSEResumeTokenLossMachine(validated_executor, timeout)

    return _run_machine(
        factory,
        seed=seed,
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
        no_failure_message="no second SSE resume token failure was generated",
    )
