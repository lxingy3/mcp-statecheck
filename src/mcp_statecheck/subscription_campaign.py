"""Controlled subscription delivery, independent oracle, and replay evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from math import isfinite
from pathlib import Path

import anyio
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule

from .execution import (
    ExecutionProtocolError,
    ExecutionResult,
    execute_http,
    execute_stdio,
)
from .invariants import Failure, failure_signature
from .model import Action, ActionKind, JsonValue
from .replay import (
    ReplayAttempt,
    ReplayInfrastructureError,
    ReplayMismatch,
    ReplayResult,
)
from .stateful import ShrinkResult, _Counterexample, _run_machine
from .trace import TraceRecorder

PROTOCOL = "2026-07-28"
SPEC = "https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions"
RESOURCE = "file:///controlled/config.json"
SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"
FIXTURES = (
    "notification-before-ack",
    "wrong-subscription-id",
    "unsolicited-notification",
)
EXPECTED_KINDS = {
    "notification-before-ack": "subscriptions.notification_before_ack",
    "wrong-subscription-id": "subscriptions.unknown_id",
    "unsolicited-notification": "subscriptions.unrequested_notification",
}
TRANSPORTS = ("stdio", "streamable-http")
RECIPE_KIND = "controlled-subscriptions"
SEED = 20260927
RUNS = 2
CHECKED = Path(__file__).resolve().parents[2] / "artifacts" / "m6-subscriptions"
FILTERS: tuple[dict[str, JsonValue], ...] = (
    {"toolsListChanged": True},
    {"resourceSubscriptions": [RESOURCE]},
    {},
)


def action(
    number: int,
    notifications: dict[str, JsonValue] | None = None,
    *,
    method: str = "subscriptions/listen",
) -> Action:
    return Action(
        f"request-{number}",
        ActionKind.REQUEST,
        mcp_request_id=number,
        method=method,
        payload={"notifications": notifications}
        if method == "subscriptions/listen"
        else {},
        protocol_version=PROTOCOL,
        capabilities={},
    )


def validate_actions(actions: Sequence[Action]) -> tuple[Action, ...]:
    if not 1 <= len(actions) <= 16:
        raise ExecutionProtocolError("subscription plan must contain 1-16 actions")
    seen_actions: set[str] = set()
    seen_ids: set[tuple[type, int | str]] = set()
    listens = 0
    for item in actions:
        if (
            not isinstance(item, Action)
            or item.kind is not ActionKind.REQUEST
            or item.protocol_version != PROTOCOL
            or item.capabilities != {}
            or item.target_action_id is not None
            or item.stream_id is not None
            or item.resume_token is not None
        ):
            raise ExecutionProtocolError("invalid controlled subscription action")
        if (
            type(item.mcp_request_id) not in (int, str)
            or not item.action_id
            or item.action_id in seen_actions
        ):
            raise ExecutionProtocolError("invalid subscription request identity")
        identity = (type(item.mcp_request_id), item.mcp_request_id)
        if identity in seen_ids:
            raise ExecutionProtocolError("subscription request ID was reused")
        seen_ids.add(identity)
        seen_actions.add(item.action_id)
        if item.method == "subscriptions/listen":
            listens += 1
            if (
                not isinstance(item.payload, dict)
                or item.payload.get("notifications") not in FILTERS
                or set(item.payload) != {"notifications"}
            ):
                raise ExecutionProtocolError("unsupported subscription filter")
        elif item.method != "tools/list" or item.payload != {}:
            raise ExecutionProtocolError("unsupported controlled subscription method")
    if listens == 0:
        raise ExecutionProtocolError("subscription plan has no listen request")
    return tuple(actions)


def _failure(actions: Sequence[Action], kind: str, method: str) -> Failure:
    evidence: dict[str, JsonValue] = {"subject": "server", "method": method}
    return Failure(
        kind, SPEC, actions[0].action_id, evidence, failure_signature(kind, evidence)
    )


def evaluate(
    actions: Sequence[Action], events: Sequence[Mapping[str, object]]
) -> Failure | None:
    """Check observed wire messages without consulting peer mode or internals."""
    subscriptions = {
        (type(item.mcp_request_id), item.mcp_request_id): item
        for item in actions
        if item.method == "subscriptions/listen"
    }
    by_action = {item.action_id: item for item in actions}
    acknowledged: dict[tuple[type, int | str], dict[str, JsonValue]] = {}
    closed: set[tuple[type, int | str]] = set()
    for event in events:
        kind = event.get("kind")
        if kind == "notification":
            method = event.get("method")
            payload = event.get("payload")
            meta = payload.get("_meta") if isinstance(payload, Mapping) else None
            raw_id = meta.get(SUBSCRIPTION_ID) if isinstance(meta, Mapping) else None
            if type(raw_id) not in (int, str):
                return _failure(actions, "subscriptions.unknown_id", str(method))
            identity = (type(raw_id), raw_id)
            if identity not in subscriptions:
                return _failure(actions, "subscriptions.unknown_id", str(method))
            if identity in closed:
                return _failure(
                    actions, "subscriptions.notification_after_close", str(method)
                )
            if method == "notifications/subscriptions/acknowledged":
                if identity in acknowledged:
                    return _failure(actions, "subscriptions.duplicate_ack", str(method))
                agreed = payload.get("notifications")
                requested = subscriptions[identity].payload["notifications"]
                if not isinstance(agreed, Mapping) or any(
                    key not in requested
                    or (key == "toolsListChanged" and value is not True)
                    or (
                        key == "resourceSubscriptions"
                        and (
                            not isinstance(value, list)
                            or not all(isinstance(uri, str) for uri in value)
                            or not set(value).issubset(set(requested.get(key, [])))
                        )
                    )
                    for key, value in agreed.items()
                ):
                    return _failure(
                        actions, "subscriptions.invalid_ack_filter", str(method)
                    )
                acknowledged[identity] = dict(agreed)
                continue
            if identity not in acknowledged:
                return _failure(
                    actions, "subscriptions.notification_before_ack", str(method)
                )
            agreed = acknowledged[identity]
            allowed = (
                method == "notifications/tools/list_changed"
                and agreed.get("toolsListChanged") is True
            ) or (
                method == "notifications/resources/updated"
                and isinstance(payload, Mapping)
                and payload.get("uri") in agreed.get("resourceSubscriptions", [])
            )
            if not allowed:
                return _failure(
                    actions, "subscriptions.unrequested_notification", str(method)
                )
        elif kind == "response":
            action_id = event.get("target_action_id")
            item = by_action.get(action_id)
            if item is None or item.method != "subscriptions/listen":
                continue
            identity = (type(item.mcp_request_id), item.mcp_request_id)
            payload = event.get("payload")
            meta = payload.get("_meta") if isinstance(payload, Mapping) else None
            if (
                identity not in acknowledged
                or event.get("outcome") != "success"
                or not isinstance(payload, Mapping)
                or payload.get("resultType") != "complete"
                or not isinstance(meta, Mapping)
                or type(meta.get(SUBSCRIPTION_ID)) is not identity[0]
                or meta[SUBSCRIPTION_ID] != identity[1]
            ):
                return _failure(
                    actions, "subscriptions.invalid_close", "subscriptions/listen"
                )
            closed.add(identity)
    if set(acknowledged) != set(subscriptions) or closed != set(subscriptions):
        return _failure(actions, "subscriptions.incomplete", "subscriptions/listen")
    return None


async def execute_fixture(
    actions: Sequence[Action], mode: str, transport: str, timeout: float = 5.0
) -> ExecutionResult:
    validate_actions(actions)
    if (
        mode not in (*FIXTURES, "conforming")
        or transport not in TRANSPORTS
        or not isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("unsupported subscription fixture, transport, or timeout")
    if transport == "stdio":
        result = await execute_stdio(
            actions,
            (
                sys.executable,
                "-I",
                "-m",
                "mcp_statecheck._subscription_peer",
                "--mode",
                mode,
            ),
            timeout=timeout,
        )
        result = replace(
            result, cleanup={"server_reaped": result.returncode is not None}
        )
        if result.returncode != 0:
            raise ReplayInfrastructureError(
                "subscription stdio peer did not exit cleanly"
            )
    else:
        from ._subscription_peer import ControlledSubscriptionHTTPPeer

        with ControlledSubscriptionHTTPPeer(mode) as peer:
            result = await execute_http(actions, peer.url, timeout=timeout)
        result = replace(
            result, cleanup={**result.cleanup, "listener_closed": peer.closed}
        )
        if not peer.closed or result.cleanup.get("client_closed") is not True:
            raise ReplayInfrastructureError(
                "subscription HTTP cleanup was not confirmed"
            )
    expected = [item.action_id for item in actions]
    responses = [event for event in result.events if event.get("kind") == "response"]
    if (
        [event.get("target_action_id") for event in responses] != expected
        or any(event.get("outcome") != "success" for event in responses)
        or any(
            event.get("kind") in {"timeout", "http_error"} for event in result.events
        )
    ):
        raise ReplayInfrastructureError("controlled subscription plan did not complete")
    return result


class _SubscriptionMachine(RuleBasedStateMachine):
    def __init__(self, mode: str, transport: str, timeout: float):
        super().__init__()
        self.mode, self.transport, self.timeout = mode, transport, timeout
        self.actions: list[Action] = []

    @initialize()
    def first_listen(self):
        self.actions.append(action(1, FILTERS[0]))

    @rule()
    def another_listen(self):
        self.actions.append(action(len(self.actions) + 1, FILTERS[1]))

    @rule()
    def unrelated_list(self):
        self.actions.append(action(len(self.actions) + 1, method="tools/list"))

    def teardown(self):
        if sys.exception() is not None:
            return
        result = anyio.run(
            execute_fixture,
            tuple(self.actions),
            self.mode,
            self.transport,
            self.timeout,
        )
        failure = evaluate(self.actions, result.events)
        if failure is not None:
            raise _Counterexample(self.actions, result, failure)


def shrink_failure(
    mode: str, transport: str, *, seed: int = SEED, timeout: float = 5.0
) -> ShrinkResult:
    if (
        mode not in FIXTURES
        or transport not in TRANSPORTS
        or type(seed) is not int
        or not isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("invalid subscription campaign options")
    return _run_machine(
        lambda: _SubscriptionMachine(mode, transport, timeout),
        seed=seed,
        max_examples=25,
        stateful_step_count=8,
        no_failure_message="no subscription delivery failure was generated",
    )


def target_recipe(mode: str) -> dict[str, JsonValue]:
    if mode not in FIXTURES:
        raise ValueError("unknown subscription fixture")
    return {"version": 2, "kind": RECIPE_KIND, "fixture_id": mode}


async def replay_subscription_artifact(
    artifact: Mapping[str, object], attempts: int, timeout: float
) -> ReplayResult:
    recipe = artifact.get("target_recipe")
    if (
        not isinstance(recipe, dict)
        or set(recipe) != {"version", "kind", "fixture_id"}
        or type(recipe.get("version")) is not int
        or recipe.get("version") != 2
        or recipe.get("kind") != RECIPE_KIND
        or recipe.get("fixture_id") not in FIXTURES
    ):
        raise ReplayInfrastructureError("invalid controlled subscription recipe")
    mode = recipe["fixture_id"]
    if (
        artifact.get("fixture_id") != mode
        or artifact.get("protocol_version") != PROTOCOL
        or artifact.get("adapter") != "subscriptions-wire"
        or artifact.get("sdk_version") != "none"
        or artifact.get("transport") not in TRANSPORTS
    ):
        raise ReplayInfrastructureError(
            "subscription artifact metadata differs from recipe"
        )
    if (
        type(attempts) is not int
        or attempts <= 0
        or not isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("invalid subscription replay attempts or timeout")
    failure = artifact.get("failure")
    if not isinstance(failure, Mapping) or not isinstance(
        failure.get("minimized_reproducer"), list
    ):
        raise ReplayInfrastructureError("subscription replay requires a reproducer")
    try:
        actions = validate_actions(
            tuple(Action.from_dict(item) for item in failure["minimized_reproducer"])
        )
    except (TypeError, ValueError, KeyError, ExecutionProtocolError) as exc:
        raise ReplayInfrastructureError("invalid subscription reproducer") from exc
    if actions[0] != action(1, FILTERS[0]):
        raise ReplayInfrastructureError(
            "subscription recipe requires its controlled first request"
        )
    completed = []
    for number in range(1, attempts + 1):
        result = await execute_fixture(actions, mode, artifact["transport"], timeout)
        observed = evaluate(actions, result.events)
        if observed is None or observed.signature != failure.get("signature"):
            raise ReplayMismatch(
                f"subscription replay {number} did not reproduce the expected signature"
            )
        completed.append(ReplayAttempt(number, result, observed))
    return ReplayResult(failure["signature"], tuple(completed))


def build_artifact(
    output: Path,
    *,
    fixture_id: str,
    transport: str,
    seed: int = SEED,
    timeout: float = 5.0,
) -> Path:
    found = shrink_failure(fixture_id, transport, seed=seed, timeout=timeout)
    recorder = TraceRecorder(
        protocol_version=PROTOCOL,
        adapter="subscriptions-wire",
        sdk_version="none",
        transport=transport,
        seed=seed,
        fixture_id=fixture_id,
        environment=os.environ,
        cleanup=found.execution.cleanup,
        target_recipe=target_recipe(fixture_id),
        generation={
            "engine": "Hypothesis RuleBasedStateMachine",
            "version": found.hypothesis_version,
            "settings": found.settings,
            "profile": "subscriptions",
        },
    )
    for item in found.actions:
        recorder.record_action(item.to_dict())
    for event in found.execution.events:
        recorder.record_event(event)
    recorder.set_failure(
        kind=found.failure.kind,
        spec_reference=found.failure.spec_reference,
        signature=found.failure.signature,
        trigger_action_id=found.failure.trigger_action_id,
        evidence=found.failure.evidence,
        minimized_reproducer=[item.to_dict() for item in found.actions],
    )
    from .replay import replay_artifact

    with tempfile.TemporaryDirectory(prefix="mcp-subscription-replay-") as temporary:
        path = recorder.write(Path(temporary) / "failure.json")
        replayed = anyio.run(replay_artifact, path, 10, timeout)
    recorder.set_replay(
        attempts=10,
        matched=10,
        signature=replayed.expected_signature,
        returncodes=[item.execution.returncode for item in replayed.attempts],
        cleanups=[item.execution.cleanup for item in replayed.attempts],
    )
    return recorder.write(output)


def _run_once(directory: Path) -> tuple[dict[str, bytes], dict[str, object]]:
    files: dict[str, bytes] = {}
    rows: dict[str, object] = {}
    wire_by_mode: dict[str, list[object]] = {}
    for mode in FIXTURES:
        for transport in TRANSPORTS:
            name = f"{transport}/{mode}.json"
            path = build_artifact(
                directory / name, fixture_id=mode, transport=transport
            )
            value = json.loads(path.read_text(encoding="utf-8"))
            failure = value["failure"]
            actions = tuple(
                Action.from_dict(item) for item in failure["minimized_reproducer"]
            )
            events = value["normalized_events"]
            observed = evaluate(actions, events)
            if (
                observed is None
                or observed.kind != EXPECTED_KINDS[mode]
                or observed.signature != failure["signature"]
                or len(actions) != 1
                or value["replay"]["matched"] != 10
            ):
                raise ReplayInfrastructureError(
                    "subscription acceptance evidence differs from observed failure"
                )
            if mode in wire_by_mode and events != wire_by_mode[mode]:
                raise ReplayInfrastructureError(
                    "subscription failure wire differed across transports"
                )
            wire_by_mode[mode] = events
            files[name] = path.read_bytes()
            rows[name] = {
                "failure_kind": observed.kind,
                "minimized_actions": len(actions),
                "signature": observed.signature,
                "replays": 10,
                "sha256": hashlib.sha256(files[name]).hexdigest(),
            }
    healthy: dict[str, object] = {}
    healthy_wire: dict[str, object] = {}
    for scenario, plan in {
        "tools": (action(1, FILTERS[0]),),
        "resource": (action(1, FILTERS[1]),),
        "none": (action(1, FILTERS[2]),),
        "two": (action(1, FILTERS[0]), action(2, FILTERS[1])),
    }.items():
        for transport in TRANSPORTS:
            result = anyio.run(execute_fixture, plan, "conforming", transport)
            if evaluate(plan, result.events) is not None:
                raise ReplayInfrastructureError(
                    "conforming subscription peer produced a false positive"
                )
            if scenario in healthy_wire and result.events != healthy_wire[scenario]:
                raise ReplayInfrastructureError(
                    "healthy subscription wire differed across transports"
                )
            healthy_wire[scenario] = result.events
            methods = [
                event.get("method")
                for event in result.events
                if event["kind"] == "notification"
            ]
            notices = sum(
                method != "notifications/subscriptions/acknowledged"
                for method in methods
            )
            if notices != (2 if scenario == "two" else 0 if scenario == "none" else 1):
                raise ReplayInfrastructureError(
                    "healthy subscription notifications were incomplete"
                )
            healthy[f"{transport}/{scenario}"] = {
                "notification_methods": methods,
                "cleanup": result.cleanup,
            }
    return files, {"traces": rows, "healthy": healthy}


def run_acceptance(output: Path, *, check: bool) -> dict[str, object]:
    with tempfile.TemporaryDirectory(
        prefix="mcp-subscriptions-acceptance-"
    ) as temporary:
        temporary_path = Path(temporary)
        first, first_rows = _run_once(temporary_path / "run-01")
        second, second_rows = _run_once(temporary_path / "run-02")
        if first != second or first_rows != second_rows:
            raise ReplayInfrastructureError(
                "subscription campaigns were not byte-identical"
            )
        summary: dict[str, object] = {
            "schema_version": 1,
            "milestone": "M6.3",
            "slice": "controlled-subscription-delivery",
            "protocol_version": PROTOCOL,
            "status": "passed",
            "seed": SEED,
            "runs": RUNS,
            "generated_cells": len(first),
            "healthy_cells": len(first_rows["healthy"]),
            **first_rows,
        }
        files = {
            **first,
            "acceptance.json": (
                json.dumps(summary, indent=2, sort_keys=True) + "\n"
            ).encode(),
        }
        if check:
            observed = {
                path.relative_to(CHECKED).as_posix(): path.read_bytes()
                for path in CHECKED.rglob("*")
                if path.is_file()
            }
            if files != observed:
                raise ReplayInfrastructureError(
                    "subscription results differ from checked evidence"
                )
        else:
            for name, content in files.items():
                destination = output / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recheck M6.3 subscription delivery evidence"
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=CHECKED)
    args = parser.parse_args()
    summary = run_acceptance(args.output, check=args.check)
    print(
        f"M6.3 subscriptions passed: {summary['generated_cells']} generated and {summary['healthy_cells']} healthy cells across {RUNS} byte-identical runs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
