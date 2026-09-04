"""Sequential Tasks execution with handles bound from observed creation results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from math import isfinite

import anyio

from .execution import (
    ExecutionProtocolError,
    ExecutionResult,
    _http_status_event,
    _http_timeout_event,
    _id_key,
    _normalize_inbound,
    _wire_message,
)
from .model import Action, ActionKind, JsonValue
from .transports import (
    HTTPStatusError,
    HTTPTimeout,
    StdioTransport,
    StreamableHTTPTransport,
)

TASK_METHODS = frozenset({"tasks/get", "tasks/update", "tasks/cancel"})


def validate_task_actions(actions: Sequence[Action]) -> tuple[Action, ...]:
    """Reject malformed plans before opening any transport."""
    if not actions or len(actions) > 128:
        raise ExecutionProtocolError("Tasks plans require between 1 and 128 actions")
    seen: dict[str, Action] = {}
    request_ids: set[str] = set()
    for action in actions:
        if not isinstance(action, Action):
            raise ExecutionProtocolError("Tasks plans require canonical actions")
        if action.action_id in seen:
            raise ExecutionProtocolError("Tasks action IDs must be unique")
        if action.kind is not ActionKind.REQUEST or action.method not in {
            "tools/call",
            "tools/list",
            *TASK_METHODS,
        }:
            raise ExecutionProtocolError("unsupported Tasks action")
        if action.protocol_version != "2026-07-28":
            raise ExecutionProtocolError("Tasks require protocol 2026-07-28")
        if type(action.mcp_request_id) is not int and not isinstance(
            action.mcp_request_id, str
        ):
            raise ExecutionProtocolError(
                "Tasks request IDs must be integers or strings"
            )
        key = _id_key(action.mcp_request_id)
        if key in request_ids:
            raise ExecutionProtocolError("Tasks request IDs must be unique")
        request_ids.add(key)
        if not isinstance(action.payload, dict):
            raise ExecutionProtocolError("Tasks action payload must be an object")
        if action.stream_id is not None or action.resume_token is not None:
            raise ExecutionProtocolError("Tasks plans do not support stream actions")
        if action.method in TASK_METHODS:
            creator = seen.get(action.target_action_id)
            if creator is None or creator.method != "tools/call":
                raise ExecutionProtocolError(
                    "Tasks reference must name an earlier tools/call"
                )
            if "taskId" in action.payload:
                raise ExecutionProtocolError(
                    "taskId must be bound from a creation response"
                )
            if action.method == "tasks/update":
                if set(action.payload) != {"inputResponses"} or not isinstance(
                    action.payload.get("inputResponses"), dict
                ):
                    raise ExecutionProtocolError("tasks/update requires inputResponses")
            elif action.payload:
                raise ExecutionProtocolError(
                    "Tasks get/cancel payload must be empty before binding"
                )
        elif action.target_action_id is not None:
            raise ExecutionProtocolError(
                "only Tasks management actions may reference a task"
            )
        _wire_message(action)
        seen[action.action_id] = action
    return tuple(actions)


async def execute_task_actions(
    actions: Sequence[Action],
    *,
    command: Sequence[str] | None = None,
    url: str | None = None,
    timeout: float = 5.0,
) -> ExecutionResult:
    """Execute one bounded plan; each task reference uses a real peer response."""
    canonical = validate_task_actions(actions)
    if (command is None) == (url is None):
        raise ValueError("specify exactly one Tasks transport target")
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    transport = (
        StdioTransport(command, timeout=timeout)
        if command is not None
        else StreamableHTTPTransport(url, timeout=timeout)
    )
    events: list[dict[str, JsonValue]] = []
    handles: dict[str, str] = {}
    # Startup has the transport's own deadline. Keep the plan scope inside its
    # lifetime so cancellation unwinds before the transport reaps its child.
    async with transport:
        with anyio.fail_after(timeout):
            for action in canonical:
                bound = action
                if action.method in TASK_METHODS:
                    handle = handles.get(action.target_action_id)
                    if handle is None:
                        # Keep the malformed creation evidence for the oracle and
                        # never invent a handle or send a dependent request.
                        break
                    bound = replace(
                        action, payload={**action.payload, "taskId": handle}
                    )
                message = _wire_message(bound)
                pending = {_id_key(action.mcp_request_id): [action.action_id]}
                if isinstance(transport, StdioTransport):
                    await transport.send(message)
                    inbound = []
                    while pending:
                        event = _normalize_inbound(await transport.receive(), pending)
                        if event["kind"] == "server_request":
                            raise ExecutionProtocolError(
                                "modern Tasks peer initiated a request"
                            )
                        inbound.append(event)
                else:
                    try:
                        messages = await transport.send(message)
                    except HTTPStatusError as exc:
                        events.append(
                            _http_status_event(action, "POST", exc.status_code)
                        )
                        break
                    except HTTPTimeout as exc:
                        events.append(
                            _http_timeout_event(action, "POST", exc.status_code)
                        )
                        break
                    inbound = [_normalize_inbound(item, pending) for item in messages]
                    if pending or any(
                        item["kind"] == "server_request" for item in inbound
                    ):
                        raise ExecutionProtocolError(
                            "invalid modern Tasks response sequence"
                        )
                events.extend(inbound)
                response = next(item for item in inbound if item["kind"] == "response")
                payload = response["payload"]
                if (
                    action.method == "tools/call"
                    and response["outcome"] == "success"
                    and isinstance(payload, Mapping)
                    and payload.get("resultType") == "task"
                    and isinstance(payload.get("taskId"), str)
                    and payload["taskId"]
                ):
                    handles[action.action_id] = payload["taskId"]
    if isinstance(transport, StdioTransport):
        return ExecutionResult(
            tuple(events),
            transport.returncode,
            transport.stderr,
            {"server_reaped": transport.returncode is not None},
        )
    return ExecutionResult(tuple(events), None, "", {"client_closed": True})
