"""Shrink and replay one complete M2 controlled failure."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio

from mcp_statecheck._controlled_peer import execute_controlled_http_fault
from mcp_statecheck.fixtures import (
    FIXTURES,
    HTTP_ERROR_FIXTURE_ID,
    SSE_RESUME_FIXTURE_ID,
    fixture_by_id,
)
from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.replay import (
    REPLAY_ATTEMPTS,
    controlled_target_recipe,
    replay_artifact,
)
from mcp_statecheck.stateful import (
    ShrinkResult,
    shrink_duplicate_request_id,
    shrink_http_error_as_timeout,
    shrink_late_response_correlation,
    shrink_request_before_initialize,
    shrink_second_sse_resume_token_loss,
)
from mcp_statecheck.trace import TraceRecorder

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_ID = "request-before-initialized"
M2_FIXTURE_IDS = tuple(fixture.fixture_id for fixture in FIXTURES)
DEFAULT_OUTPUTS = {
    fixture_id: ROOT / "artifacts" / "m2" / f"{fixture_id}.json"
    for fixture_id in M2_FIXTURE_IDS
}
DEFAULT_SEEDS = {
    HTTP_ERROR_FIXTURE_ID: 20_260_721,
    "request-before-initialized": 20_260_716,
    "duplicate-concurrent-request-id": 20_260_717,
    "late-response-after-cancellation": 20_260_720,
    SSE_RESUME_FIXTURE_ID: 20_260_722,
}


def _controlled_http_executor(fixture_id: str):
    async def execute(
        actions: tuple[Action, ...],
        timeout: float,
    ):
        return await execute_controlled_http_fault(actions, fixture_id, timeout)

    return execute


def _peer_command(fixture_id: str) -> tuple[str, ...]:
    return (
        sys.executable,
        "-I",
        "-m",
        "mcp_statecheck._controlled_peer",
        "--stdio",
        "--mode",
        fixture_id,
    )


def _recorder(result: ShrinkResult, fixture_id: str) -> TraceRecorder:
    fixture = fixture_by_id(fixture_id)
    cleanup = result.execution.cleanup or {
        "shrink_peer_reaped": result.execution.returncode is not None,
        "shrink_peer_returncode": result.execution.returncode,
    }
    recorder = TraceRecorder(
        protocol_version="2025-11-25",
        adapter=(
            "controlled-wire" if fixture.transport == "streamable-http" else "wire"
        ),
        sdk_version="none",
        transport=fixture.transport,
        seed=result.seed,
        fixture_id=fixture_id,
        cleanup=cleanup,
        generation={
            "engine": "Hypothesis RuleBasedStateMachine",
            "settings": result.settings,
            "version": result.hypothesis_version,
        },
        target_recipe=controlled_target_recipe(fixture_id),
    )
    events = iter(result.execution.events)
    event = next(events, None)
    for action in result.actions:
        recorder.record_action(action.to_dict())
        if fixture.transport == "streamable-http":
            while (
                event is not None and event.get("target_action_id") == action.action_id
            ):
                recorder.record_event(event)
                event = next(events, None)
        elif action.kind is ActionKind.RESPONSE:
            if event is None:
                raise RuntimeError("response action has no normalized event")
            recorder.record_event(event)
            event = next(events, None)
    if event is not None:
        recorder.record_event(event)
    for remaining_event in events:
        recorder.record_event(remaining_event)
    recorder.set_failure(
        kind=result.failure.kind,
        spec_reference=result.failure.spec_reference,
        signature=result.failure.signature,
        minimized_reproducer=tuple(action.to_dict() for action in result.actions),
        trigger_action_id=result.failure.trigger_action_id,
        evidence=result.failure.evidence,
    )
    return recorder


def _shrink(
    fixture_id: str,
    *,
    seed: int,
    timeout: float,
) -> ShrinkResult:
    if fixture_id in {HTTP_ERROR_FIXTURE_ID, SSE_RESUME_FIXTURE_ID}:
        executor = _controlled_http_executor(fixture_id)
        if fixture_id == HTTP_ERROR_FIXTURE_ID:
            return shrink_http_error_as_timeout(executor, seed=seed, timeout=timeout)
        return shrink_second_sse_resume_token_loss(
            executor,
            seed=seed,
            timeout=timeout,
        )
    command = _peer_command(fixture_id)
    if fixture_id == "request-before-initialized":
        return shrink_request_before_initialize(
            command,
            seed=seed,
            timeout=timeout,
        )
    if fixture_id == "duplicate-concurrent-request-id":
        return shrink_duplicate_request_id(
            command,
            seed=seed,
            timeout=timeout,
        )
    if fixture_id == "late-response-after-cancellation":
        return shrink_late_response_correlation(
            command,
            seed=seed,
            timeout=timeout,
        )
    raise ValueError(f"unsupported M2 fixture: {fixture_id}")


def build_artifact(
    output: Path,
    *,
    fixture_id: str = DEFAULT_FIXTURE_ID,
    seed: int | None = None,
    timeout: float = 5.0,
) -> Path:
    """Write, reload, replay, and finalize the deterministic M2 trace."""

    if seed is None:
        try:
            seed = DEFAULT_SEEDS[fixture_id]
        except KeyError as exc:
            raise ValueError(f"unsupported M2 fixture: {fixture_id}") from exc
    result = _shrink(
        fixture_id,
        seed=seed,
        timeout=timeout,
    )
    recorder = _recorder(result, fixture_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        dir=output.parent,
        prefix=f".{output.name}.replay.",
    ) as temporary_directory:
        staging = Path(temporary_directory) / output.name
        recorder.write(staging)
        replay = anyio.run(
            replay_artifact,
            staging,
            REPLAY_ATTEMPTS,
            timeout,
        )
    recorder.set_replay(
        attempts=len(replay.attempts),
        matched=len(replay.attempts),
        signature=replay.expected_signature,
        returncodes=tuple(attempt.execution.returncode for attempt in replay.attempts),
        cleanups=(
            tuple(attempt.execution.cleanup for attempt in replay.attempts)
            if fixture_by_id(fixture_id).transport == "streamable-http"
            else None
        ),
    )
    recorder.write(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture",
        choices=M2_FIXTURE_IDS,
        default=DEFAULT_FIXTURE_ID,
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    output = build_artifact(
        args.output or DEFAULT_OUTPUTS[args.fixture],
        fixture_id=args.fixture,
        seed=args.seed,
        timeout=args.timeout,
    )
    resolved_output = output.resolve()
    try:
        display_output = resolved_output.relative_to(ROOT).as_posix()
    except ValueError:
        display_output = str(resolved_output)
    print(
        f"M2 slice passed: minimized and replayed {REPLAY_ATTEMPTS} times; "
        f"wrote {display_output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
