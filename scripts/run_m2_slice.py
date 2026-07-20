"""Shrink and replay one complete M2 controlled failure."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio

from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.replay import ReplayResult, replay_stdio_failure
from mcp_statecheck.stateful import (
    ShrinkResult,
    shrink_duplicate_request_id,
    shrink_late_response_correlation,
    shrink_request_before_initialize,
)
from mcp_statecheck.trace import TraceRecorder

ROOT = Path(__file__).resolve().parents[1]
PEER = ROOT / "tests" / "fixtures" / "peer.py"
DEFAULT_FIXTURE_ID = "request-before-initialized"
M2_FIXTURE_IDS = (
    DEFAULT_FIXTURE_ID,
    "duplicate-concurrent-request-id",
    "late-response-after-cancellation",
)
DEFAULT_OUTPUTS = {
    fixture_id: ROOT / "artifacts" / "m2" / f"{fixture_id}.json"
    for fixture_id in M2_FIXTURE_IDS
}
DEFAULT_SEEDS = {
    "request-before-initialized": 20_260_716,
    "duplicate-concurrent-request-id": 20_260_717,
    "late-response-after-cancellation": 20_260_720,
}
REPLAY_ATTEMPTS = 10


def _peer_command(fixture_id: str) -> tuple[str, ...]:
    return (
        sys.executable,
        str(PEER),
        "--stdio",
        "--mode",
        fixture_id,
    )


def _recorder(result: ShrinkResult, fixture_id: str) -> TraceRecorder:
    recorder = TraceRecorder(
        protocol_version="2025-11-25",
        adapter="wire",
        sdk_version="none",
        transport="stdio",
        seed=result.seed,
        fixture_id=fixture_id,
        cleanup={
            "shrink_peer_reaped": result.execution.returncode is not None,
            "shrink_peer_returncode": result.execution.returncode,
        },
        generation={
            "engine": "Hypothesis RuleBasedStateMachine",
            "settings": result.settings,
            "version": result.hypothesis_version,
        },
    )
    events = iter(result.execution.events)
    for action in result.actions:
        recorder.record_action(action.to_dict())
        if action.kind is ActionKind.RESPONSE:
            recorder.record_event(next(events))
    for event in events:
        recorder.record_event(event)
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
    command: tuple[str, ...],
    *,
    seed: int,
    timeout: float,
) -> ShrinkResult:
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


def _load_failure(path: Path) -> tuple[tuple[Action, ...], str]:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(artifact, Mapping):
        raise TypeError("saved trace must be an object")
    failure = artifact.get("failure")
    if not isinstance(failure, Mapping):
        raise TypeError("saved trace must contain a failure object")
    reproducer = failure.get("minimized_reproducer")
    if not isinstance(reproducer, list) or not all(
        isinstance(action, Mapping) for action in reproducer
    ):
        raise TypeError("saved trace must contain canonical reproducer actions")
    signature = failure.get("signature")
    if not isinstance(signature, str) or not signature:
        raise TypeError("saved trace must contain a failure signature")
    return tuple(Action.from_dict(action) for action in reproducer), signature


async def _replay_saved(
    actions: tuple[Action, ...],
    signature: str,
    command: tuple[str, ...],
    timeout: float,
    fixture_id: str,
) -> ReplayResult:
    return await replay_stdio_failure(
        actions,
        command,
        expected_signature=signature,
        fixture_id=fixture_id,
        attempts=REPLAY_ATTEMPTS,
        timeout=timeout,
    )


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
    command = _peer_command(fixture_id)
    result = _shrink(
        fixture_id,
        command,
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
        saved_actions, saved_signature = _load_failure(staging)
        replay = anyio.run(
            _replay_saved,
            saved_actions,
            saved_signature,
            command,
            timeout,
            fixture_id,
        )
    recorder.set_replay(
        attempts=len(replay.attempts),
        matched=len(replay.attempts),
        signature=replay.expected_signature,
        returncodes=tuple(attempt.execution.returncode for attempt in replay.attempts),
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
