"""Verify generated Tasks failures and conforming wire baselines."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import anyio

from mcp_statecheck.model import Action
from mcp_statecheck.task_campaign import (
    TASK_FIXTURES,
    TASK_TRANSPORTS,
    build_task_artifact,
    execute_task_fixture,
    task_baseline,
)
from mcp_statecheck.tasks import PROTOCOL_VERSION, evaluate_tasks

if __package__:
    from .run_m4_acceptance import AcceptanceError, _atomic_write
else:
    from run_m4_acceptance import AcceptanceError, _atomic_write

if __package__:
    from .run_m6_modern_acceptance import _copy_file, _load_object
else:
    from run_m6_modern_acceptance import _copy_file, _load_object

ROOT = Path(__file__).resolve().parents[1]
CHECKED_ARTIFACTS = ROOT / "artifacts" / "m6-tasks"
DEFAULT_OUTPUT = Path("artifacts/m6-tasks")
RUNS = 2
SEED = 20260904
EXPECTED_FIXTURES = {
    "task-terminal-regression": ("complete", 3),
    "task-input-key-reuse": ("input", 4),
    "task-result-shape": ("complete", 2),
}
TERMINAL_STATUSES = {
    "complete": "completed",
    "input": "completed",
    "cancel": "cancelled",
    "fail": "failed",
    "tool_error": "completed",
}
BASELINE_TRANSITIONS = {
    "complete": ("created->working", "working->completed"),
    "input": (
        "created->working",
        "working->input_required",
        "input_required->working",
        "working->completed",
    ),
    "cancel": ("created->working", "working->cancelled"),
    "fail": ("created->working", "working->failed"),
    "tool_error": ("created->working", "working->completed"),
}
FAILURE_KINDS = {
    "task-terminal-regression": "tasks.terminal_state_changed",
    "task-input-key-reuse": "tasks.input_request_key_reused",
    "task-result-shape": "tasks.invalid_result",
}


def _expected_names() -> set[Path]:
    return {
        Path(transport) / f"{fixture}.json"
        for transport in ("stdio", "streamable-http")
        for fixture in EXPECTED_FIXTURES
    }


def _require_exact_files(directory: Path) -> None:
    observed = {
        path.relative_to(directory) for path in directory.rglob("*") if path.is_file()
    }
    if observed != {Path("acceptance.json"), *_expected_names()}:
        raise AcceptanceError(
            "Tasks evidence must contain exactly six traces and acceptance.json"
        )


def _require_cleanup(cleanup: object, transport: str) -> None:
    required = (
        ("server_reaped",)
        if transport == "stdio"
        else ("client_closed", "listener_closed")
    )
    if not isinstance(cleanup, Mapping) or any(
        cleanup.get(key) is not True for key in required
    ):
        raise AcceptanceError(f"Tasks {transport} cleanup was not confirmed")


def _without_sequence(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "sequence"}


def _wire_events(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(event, Mapping) for event in value
    ):
        raise AcceptanceError("Tasks normalized events must be a sequence of objects")
    return [_without_sequence(event) for event in value]


def _validate_trace(
    trace: Mapping[str, Any], *, fixture_id: str, transport: str
) -> dict[str, Any]:
    if fixture_id not in EXPECTED_FIXTURES or transport not in (
        "stdio",
        "streamable-http",
    ):
        raise AcceptanceError("unknown Tasks acceptance cell")
    if (
        type(trace.get("schema_version")) is not int
        or trace["schema_version"] != 1
        or trace.get("adapter") != "tasks-wire"
        or trace.get("sdk_version") != "none"
        or trace.get("protocol_version") != PROTOCOL_VERSION
        or type(trace.get("seed")) is not int
        or trace["seed"] != SEED
        or trace.get("fixture_id") != fixture_id
        or trace.get("transport") != transport
        or trace.get("target_recipe")
        != {"version": 2, "kind": "controlled-tasks", "fixture_id": fixture_id}
    ):
        raise AcceptanceError("Tasks trace metadata does not match its cell")
    _require_cleanup(trace.get("cleanup"), transport)
    failure = trace.get("failure")
    canonical = trace.get("canonical_actions")
    if (
        not isinstance(failure, Mapping)
        or not isinstance(canonical, list)
        or not all(isinstance(action, Mapping) for action in canonical)
    ):
        raise AcceptanceError("Tasks trace has no canonical failing plan")
    reproducer = failure.get("minimized_reproducer")
    if (
        not isinstance(reproducer, list)
        or len(reproducer) != EXPECTED_FIXTURES[fixture_id][1]
    ):
        raise AcceptanceError(
            "Tasks reproducer does not have the expected minimum action count"
        )
    if [_without_sequence(action) for action in canonical] != reproducer:
        raise AcceptanceError(
            "Tasks canonical actions differ from the minimized reproducer"
        )
    try:
        actions = tuple(Action.from_dict(action) for action in reproducer)
    except (ValueError, TypeError, KeyError) as exc:
        raise AcceptanceError("Tasks reproducer contains invalid actions") from exc
    events = _wire_events(trace.get("normalized_events"))
    if len(events) != len(actions) or any(
        event.get("kind") != "response"
        or event.get("target_action_id") != action.action_id
        or event.get("mcp_request_id") != action.mcp_request_id
        or type(event.get("mcp_request_id")) is not type(action.mcp_request_id)
        or event.get("outcome") != "success"
        for action, event in zip(actions, events, strict=True)
    ):
        raise AcceptanceError(
            "Tasks trace does not contain one matched response per action"
        )
    evaluation = evaluate_tasks(actions, events)
    if evaluation.failure is None or (
        evaluation.failure.kind != FAILURE_KINDS[fixture_id]
        or failure.get("kind") != evaluation.failure.kind
        or failure.get("signature") != evaluation.failure.signature
    ):
        raise AcceptanceError(
            "Tasks wire evidence does not match the claimed failure signature"
        )
    generation = trace.get("generation")
    if not isinstance(generation, Mapping) or (
        generation.get("engine") != "Hypothesis RuleBasedStateMachine"
        or generation.get("profile") != "tasks"
        or generation.get("task_count") != evaluation.task_count
        or type(generation.get("task_count")) is not int
        or evaluation.task_count != 1
        or generation.get("transitions") != list(evaluation.transitions)
    ):
        raise AcceptanceError(
            "Tasks generation coverage differs from observed transitions"
        )
    replay = trace.get("replay")
    returncode = 0 if transport == "stdio" else None
    if not isinstance(replay, Mapping) or (
        replay.get("attempts") != 10
        or replay.get("matched") != 10
        or replay.get("signature") != evaluation.failure.signature
        or replay.get("returncodes") != [returncode] * 10
        or any(type(code) is not type(returncode) for code in replay["returncodes"])
        or not isinstance(replay.get("cleanups"), list)
        or len(replay["cleanups"]) != 10
    ):
        raise AcceptanceError(
            "Tasks replay must match ten times with successful return codes"
        )
    for cleanup in replay["cleanups"]:
        _require_cleanup(cleanup, transport)
    return {
        "fixture_id": fixture_id,
        "transport": transport,
        "minimized_action_count": len(actions),
        "signature": evaluation.failure.signature,
        "failure_kind": evaluation.failure.kind,
        "replay_matched": 10,
        "transitions": list(evaluation.transitions),
        "task_count": evaluation.task_count,
    }


def _compare_payloads(
    first: Mapping[Path, bytes], other: Mapping[Path, bytes], *, label: str
) -> None:
    if first.keys() != other.keys():
        raise AcceptanceError(f"Tasks {label} file set changed")
    if first != other:
        raise AcceptanceError(f"Tasks {label} artifacts were not byte-identical")


def _validate_baseline(
    actions: Sequence[Action],
    events: Sequence[Mapping[str, Any]],
    *,
    scenario: str,
    transport: str,
    cleanup: Mapping[str, Any],
    returncode: int | None,
) -> dict[str, Any]:
    _require_cleanup(cleanup, transport)
    if returncode != (0 if transport == "stdio" else None) or type(returncode) is not (
        int if transport == "stdio" else type(None)
    ):
        raise AcceptanceError("Tasks healthy peer did not exit successfully")
    wire = _wire_events(events)
    if len(wire) != len(actions) or any(
        event.get("kind") != "response"
        or event.get("target_action_id") != action.action_id
        or event.get("mcp_request_id") != action.mcp_request_id
        or type(event.get("mcp_request_id")) is not type(action.mcp_request_id)
        or event.get("outcome") != "success"
        for action, event in zip(actions, wire, strict=True)
    ):
        raise AcceptanceError("Tasks healthy baseline has incomplete wire responses")
    evaluation = evaluate_tasks(actions, wire)
    if evaluation.failure is not None or evaluation.task_count != 1:
        raise AcceptanceError(
            "Tasks healthy baseline reported a failure or wrong task count"
        )
    terminal = wire[-1].get("payload") if wire else None
    if (
        not isinstance(terminal, Mapping)
        or terminal.get("status") != TERMINAL_STATUSES[scenario]
    ):
        raise AcceptanceError(
            "Tasks healthy baseline did not reach its expected terminal status"
        )
    if evaluation.transitions != BASELINE_TRANSITIONS[scenario]:
        raise AcceptanceError(
            "Tasks healthy baseline skipped an expected lifecycle transition"
        )
    if scenario in {"complete", "input", "tool_error"}:
        result = terminal.get("result")
        if (
            not isinstance(result, Mapping)
            or result.get("resultType") != "complete"
            or not isinstance(result.get("content"), list)
            or not result["content"]
            or result.get("isError") is not (scenario == "tool_error")
        ):
            raise AcceptanceError(
                "Tasks healthy baseline has an unexpected tool-result payload"
            )
    elif scenario == "fail":
        error = terminal.get("error")
        if (
            not isinstance(error, Mapping)
            or error.get("code") != -32603
            or not isinstance(error.get("message"), str)
        ):
            raise AcceptanceError(
                "Tasks failed baseline has an unexpected JSON-RPC error payload"
            )
    elif "result" in terminal or "error" in terminal:
        raise AcceptanceError(
            "Tasks cancelled baseline has an unexpected terminal payload"
        )
    return {
        "scenario": scenario,
        "transport": transport,
        "terminal_status": terminal["status"],
        "task_count": evaluation.task_count,
        "transitions": list(evaluation.transitions),
        "cleanup": dict(cleanup),
        "wire_sha256": hashlib.sha256(
            json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _run_once(
    directory: Path, *, timeout: float
) -> tuple[dict[Path, bytes], dict[str, Any], dict[str, Any]]:
    payloads: dict[Path, bytes] = {}
    traces: dict[str, Any] = {}
    defect_wire: dict[str, list[dict[str, Any]]] = {}
    for fixture_id in EXPECTED_FIXTURES:
        for transport in TASK_TRANSPORTS:
            relative = Path(transport) / f"{fixture_id}.json"
            print(
                f"Tasks acceptance: {directory.name} {transport} {fixture_id}",
                file=sys.stderr,
                flush=True,
            )
            path = build_task_artifact(
                directory / relative,
                fixture_id=fixture_id,
                transport=transport,
                seed=SEED,
                timeout=timeout,
            )
            trace = _load_object(path, label="Tasks trace")
            row = _validate_trace(trace, fixture_id=fixture_id, transport=transport)
            wire = _wire_events(trace["normalized_events"])
            if fixture_id in defect_wire and wire != defect_wire[fixture_id]:
                raise AcceptanceError(
                    f"Tasks defect wire events differ across transports: {fixture_id}"
                )
            defect_wire[fixture_id] = wire
            content = path.read_bytes()
            payloads[relative] = content
            traces[relative.as_posix()] = {
                **row,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
    if payloads.keys() != _expected_names():
        raise AcceptanceError("Tasks generator did not produce its exact six traces")
    healthy: dict[str, Any] = {}
    baseline_wire: dict[str, list[dict[str, Any]]] = {}
    for scenario in TERMINAL_STATUSES:
        actions = task_baseline(scenario)
        for transport in TASK_TRANSPORTS:
            execution = anyio.run(
                execute_task_fixture, actions, "conforming", transport, timeout
            )
            row = _validate_baseline(
                actions,
                execution.events,
                scenario=scenario,
                transport=transport,
                cleanup=execution.cleanup,
                returncode=execution.returncode,
            )
            wire = _wire_events(execution.events)
            if scenario in baseline_wire and wire != baseline_wire[scenario]:
                raise AcceptanceError(
                    f"Tasks baseline wire events differ across transports: {scenario}"
                )
            baseline_wire[scenario] = wire
            healthy[f"{transport}/{scenario}"] = row
    return payloads, traces, healthy


def _summary(traces: Mapping[str, Any], healthy: Mapping[str, Any]) -> dict[str, Any]:
    rows = [*traces.values(), *healthy.values()]
    transitions = [transition for row in rows for transition in row["transitions"]]
    return {
        "schema_version": 1,
        "milestone": "M6.2",
        "slice": "generated-tasks-lifecycle",
        "status": "passed",
        "protocol_version": PROTOCOL_VERSION,
        "action_profile": "tasks",
        "seed": SEED,
        "transports": list(TASK_TRANSPORTS),
        "generated": {
            "cells": len(traces),
            "runs": RUNS,
            "byte_identical": True,
            "wire_identical_across_transports": True,
            "traces": dict(traces),
        },
        "healthy": {
            "cells": len(healthy),
            "runs": RUNS,
            "false_positives": 0,
            "wire_identical_across_transports": True,
            "scenarios": dict(healthy),
        },
        "coverage": {
            "observed_transition_count": len(transitions),
            "unique_transitions": sorted(set(transitions)),
        },
    }


def run(output: Path, *, check: bool, timeout: float = 5.0) -> dict[str, Any]:
    if TASK_FIXTURES != EXPECTED_FIXTURES or TASK_TRANSPORTS != (
        "stdio",
        "streamable-http",
    ):
        raise AcceptanceError(
            "Tasks fixture profile differs from the locked acceptance profile"
        )
    if check:
        _require_exact_files(CHECKED_ARTIFACTS)
    with tempfile.TemporaryDirectory(prefix="mcp-statecheck-m6-tasks-") as temporary:
        work = Path(temporary)
        first, traces, healthy = _run_once(work / "run-01", timeout=timeout)
        for index in range(2, RUNS + 1):
            other, other_traces, other_healthy = _run_once(
                work / f"run-{index:02d}", timeout=timeout
            )
            _compare_payloads(first, other, label="fresh run")
            if traces != other_traces or healthy != other_healthy:
                raise AcceptanceError(
                    "Tasks acceptance results changed across fresh runs"
                )
        summary = _summary(traces, healthy)
        summary_path = work / "acceptance.json"
        _atomic_write(summary_path, summary)
        generated = {**first, Path("acceptance.json"): summary_path.read_bytes()}
        if check:
            checked = {
                relative: (CHECKED_ARTIFACTS / relative).read_bytes()
                for relative in generated
            }
            _compare_payloads(generated, checked, label="checked evidence")
        for relative, content in generated.items():
            source = (
                summary_path
                if relative == Path("acceptance.json")
                else work / "run-01" / relative
            )
            if source.read_bytes() != content:
                raise AcceptanceError("Tasks evidence changed before writing")
            _copy_file(source, output / relative)
            if (output / relative).read_bytes() != content:
                raise AcceptanceError("Tasks evidence did not survive write/readback")
        _require_exact_files(output)
        return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify generated Tasks failures twice and ten healthy baselines."
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare all seven files with the checked Tasks evidence",
    )
    args = parser.parse_args(argv)
    try:
        if args.check and args.output is None:
            with tempfile.TemporaryDirectory(
                prefix="mcp-statecheck-m6-tasks-check-"
            ) as temporary:
                summary = run(Path(temporary), check=True)
            destination = "checked-in evidence"
        else:
            destination = args.output or DEFAULT_OUTPUT
            summary = run(Path(destination), check=args.check)
    except (AcceptanceError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"M6.2 Tasks acceptance failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"M6.2 Tasks acceptance passed: {summary['generated']['cells']} generated traces across {RUNS} byte-identical runs; {summary['healthy']['cells']} healthy baselines; {destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
