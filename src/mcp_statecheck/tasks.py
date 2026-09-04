"""Independent state checks for the 2026-07-28 Tasks extension.

The oracle consumes correlated wire observations. Planned response barriers and
controlled-peer configuration are not evidence of server behavior.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from .invariants import Failure, failure_signature
from .model import Action, ActionKind, JsonValue, canonical_json

PROTOCOL_VERSION = "2026-07-28"
TASK_EXTENSION = "io.modelcontextprotocol/tasks"
TASK_CAPABILITIES: dict[str, JsonValue] = {
    "extensions": {TASK_EXTENSION: {}},
    "elicitation": {"form": {}},
}
TASK_SPEC = (
    "https://tasks.extensions.modelcontextprotocol.io/specification/2026-07-28/tasks"
)
_STATUSES = {"working", "input_required", "completed", "failed", "cancelled"}
_TERMINAL = {"completed", "failed", "cancelled"}
_TASK_METHODS = {"tasks/get", "tasks/update", "tasks/cancel"}


@dataclass(frozen=True, slots=True)
class TaskEvaluation:
    failure: Failure | None
    transitions: tuple[str, ...]
    task_count: int


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _jsonrpc_error(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and type(value.get("code")) is int
        and isinstance(value.get("message"), str)
    )


def _declares_tasks(action: Action) -> bool:
    extensions = (action.capabilities or {}).get("extensions")
    return isinstance(extensions, Mapping) and isinstance(
        extensions.get(TASK_EXTENSION), Mapping
    )


def _input_requests(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    for key, request in value.items():
        if not isinstance(key, str) or not isinstance(request, Mapping):
            return False
        method = request.get("method")
        if not isinstance(method, str) or method not in {
            "elicitation/create",
            "sampling/createMessage",
            "roots/list",
        }:
            return False
        if not isinstance(request.get("params", {}), Mapping):
            return False
    return True


def _input_capability(action: Action, request: Mapping[str, object]) -> bool:
    capabilities = action.capabilities or {}
    method = request["method"]
    if method == "elicitation/create":
        supported = capabilities.get("elicitation")
        params = request.get("params", {})
        mode = params.get("mode", "form")
        return (
            isinstance(supported, Mapping)
            and isinstance(mode, str)
            and isinstance(supported.get(mode), Mapping)
        )
    key = "sampling" if method == "sampling/createMessage" else "roots"
    return isinstance(capabilities.get(key), Mapping)


def _task_shape(payload: Mapping[str, object], *, creation: bool) -> str | None:
    if payload.get("resultType") != ("task" if creation else "complete"):
        return "resultType"
    if not isinstance(payload.get("taskId"), str) or not payload["taskId"]:
        return "taskId"
    status = payload.get("status")
    if not isinstance(status, str) or status not in _STATUSES:
        return "status"
    for name in ("createdAt", "lastUpdatedAt"):
        if _timestamp(payload.get(name)) is None:
            return name
    if "ttlMs" not in payload or (
        payload["ttlMs"] is not None
        and (type(payload["ttlMs"]) is not int or payload["ttlMs"] < 0)
    ):
        return "ttlMs"
    if "pollIntervalMs" in payload and (
        type(payload["pollIntervalMs"]) is not int or payload["pollIntervalMs"] < 0
    ):
        return "pollIntervalMs"
    if "statusMessage" in payload and not isinstance(payload["statusMessage"], str):
        return "statusMessage"
    if "_meta" in payload and not isinstance(payload["_meta"], Mapping):
        return "_meta"
    if "result" in payload and not isinstance(payload["result"], Mapping):
        return "result"
    if "error" in payload and not _jsonrpc_error(payload["error"]):
        return "error"
    if "inputRequests" in payload and not _input_requests(payload["inputRequests"]):
        return "inputRequests"
    if not creation:
        if status == "completed" and not isinstance(payload.get("result"), Mapping):
            return "result"
        if status == "failed" and not _jsonrpc_error(payload.get("error")):
            return "error"
        if status == "input_required" and not _input_requests(
            payload.get("inputRequests")
        ):
            return "inputRequests"
    return None


def _failure(action: Action, kind: str, **details: JsonValue) -> Failure:
    evidence: dict[str, JsonValue] = {
        "subject": "server",
        "method": action.method,
        **details,
    }
    return Failure(
        kind=kind,
        spec_reference=TASK_SPEC,
        trigger_action_id=action.action_id,
        evidence=evidence,
        signature=failure_signature(kind, evidence),
    )


def _terminal_change(
    previous: Mapping[str, object], current: Mapping[str, object]
) -> bool:
    if previous["status"] not in _TERMINAL or previous["status"] == current["status"]:
        return False
    # The terminal-state diagram has an explicit TTL exception: a server may
    # mark a task failed after expiry. Compare only observed server timestamps;
    # replay wall time must never decide whether the same trace is a failure.
    ttl = current["ttlMs"]
    if current["status"] == "failed" and type(ttl) is int:
        created = _timestamp(current["createdAt"])
        updated = _timestamp(current["lastUpdatedAt"])
        if created is not None and updated is not None:
            try:
                if (updated - created).total_seconds() * 1000 >= ttl:
                    return False
            except TypeError:
                # A missing timezone prevents a reliable expiry comparison.
                return False
    return True


def evaluate_tasks(
    actions: Sequence[Action], events: Sequence[Mapping[str, object]]
) -> TaskEvaluation:
    """Evaluate a serial task trace without executing actions or reading clocks."""
    requests = {action.action_id: action for action in actions}
    tasks: dict[str, Mapping[str, object]] = {}
    input_history: dict[str, dict[str, JsonValue]] = {}
    delivered: dict[str, set[str]] = {}
    retired: dict[str, set[str]] = {}
    transitions: list[str] = []

    def finish(failure: Failure | None = None) -> TaskEvaluation:
        return TaskEvaluation(
            failure,
            tuple(transitions),
            len({value["taskId"] for value in tasks.values()}),
        )

    for event in events:
        if event.get("kind") != "response":
            continue
        action = requests.get(event.get("target_action_id"))
        if action is None or action.kind is not ActionKind.REQUEST:
            continue
        payload = event.get("payload")
        if action.method in _TASK_METHODS and not _declares_tasks(action):
            if not (
                event.get("outcome") == "error"
                and _jsonrpc_error(payload)
                and payload["code"] == -32021
            ):
                return finish(_failure(action, "tasks.capability_not_declared"))
            continue
        if event.get("outcome") != "success":
            continue
        if not isinstance(payload, Mapping):
            if action.method in _TASK_METHODS | {"tools/call"}:
                return finish(_failure(action, "tasks.invalid_result", field="result"))
            continue
        if (
            action.method in _TASK_METHODS or payload.get("resultType") == "task"
        ) and not _declares_tasks(action):
            return finish(_failure(action, "tasks.capability_not_declared"))
        if payload.get("resultType") == "task" and action.method not in {
            "tools/call",
            "tasks/get",
            "tasks/update",
            "tasks/cancel",
        }:
            return finish(_failure(action, "tasks.unsupported_creation_method"))
        if action.method == "tools/call" and payload.get("resultType") != "task":
            if payload.get("resultType") == "complete" and not isinstance(
                payload.get("content"), list
            ):
                return finish(_failure(action, "tasks.invalid_result", field="content"))
            if not isinstance(payload.get("resultType"), str):
                return finish(
                    _failure(action, "tasks.invalid_result", field="resultType")
                )
            continue
        if action.method in {"tasks/update", "tasks/cancel"}:
            if (
                payload.get("resultType") != "complete"
                or set(payload) - {"resultType", "_meta"}
                or ("_meta" in payload and not isinstance(payload["_meta"], Mapping))
            ):
                return finish(_failure(action, "tasks.invalid_acknowledgement"))
            if action.method == "tasks/update" and action.target_action_id in tasks:
                params = action.payload
                inputs = (
                    params.get("inputResponses")
                    if isinstance(params, Mapping)
                    else None
                )
                if isinstance(inputs, Mapping):
                    pending = tasks[action.target_action_id].get("inputRequests", {})
                    if isinstance(pending, Mapping):
                        delivered[action.target_action_id].update(
                            inputs.keys() & pending.keys()
                        )
            continue
        if action.method == "tools/call" and payload.get("resultType") == "task":
            invalid = _task_shape(payload, creation=True)
            if invalid:
                return finish(_failure(action, "tasks.invalid_result", field=invalid))
            tasks[action.action_id] = payload
            alias = next(
                (
                    key
                    for key, value in tasks.items()
                    if key != action.action_id and value["taskId"] == payload["taskId"]
                ),
                None,
            )
            input_history[action.action_id] = input_history[alias] if alias else {}
            delivered[action.action_id] = delivered[alias] if alias else set()
            retired[action.action_id] = retired[alias] if alias else set()
            transitions.append(f"created->{payload['status']}")
        elif action.method == "tasks/get":
            invalid = _task_shape(payload, creation=False)
            if invalid:
                return finish(_failure(action, "tasks.invalid_result", field=invalid))
            previous = tasks.get(action.target_action_id)
            if previous is not None:
                if previous["taskId"] != payload["taskId"]:
                    return finish(_failure(action, "tasks.identity_changed"))
                if _terminal_change(previous, payload):
                    return finish(
                        _failure(
                            action,
                            "tasks.terminal_state_changed",
                            previous_status=previous["status"],
                            status=payload["status"],
                        ),
                    )
                history = input_history[action.target_action_id]
                inputs = payload.get("inputRequests", {})
                if isinstance(inputs, Mapping):
                    retired[action.target_action_id].update(
                        delivered[action.target_action_id] - inputs.keys()
                    )
                    for key, value in inputs.items():
                        if not _input_capability(action, value):
                            return finish(
                                _failure(action, "tasks.input_capability_not_declared")
                            )
                        canonical = canonical_json(value)
                        if (
                            key in history and history[key] != canonical
                        ) or key in retired[action.target_action_id]:
                            return finish(
                                _failure(action, "tasks.input_request_key_reused")
                            )
                        history[key] = canonical
                if previous["status"] != payload["status"]:
                    transitions.append(f"{previous['status']}->{payload['status']}")
                for key, value in tasks.items():
                    if value["taskId"] == payload["taskId"]:
                        tasks[key] = payload
    return finish()
