"""Execute canonical actions at the wire boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import __version__
from .model import Action, ActionKind, JsonValue, canonical_json
from .transports import StdioTransport


class ExecutionProtocolError(Exception):
    """A canonical action or peer response cannot be executed safely."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    events: tuple[dict[str, JsonValue], ...]
    returncode: int | None
    stderr: str


async def execute_stdio(
    actions: Sequence[Action], command: Sequence[str], *, timeout: float = 5.0
) -> ExecutionResult:
    """Execute initialize and request actions against one stdio peer."""

    prepared: list[tuple[Action, dict[str, JsonValue]]] = []
    action_ids: set[str] = set()
    for action in actions:
        if not isinstance(action, Action):
            raise TypeError("actions must contain Action values")
        if action.action_id in action_ids:
            raise ExecutionProtocolError(
                f"duplicate internal action_id: {action.action_id}"
            )
        action_ids.add(action.action_id)
        prepared.append((action, _wire_message(action)))

    pending: dict[str, list[str]] = {}
    response_count = 0
    events: list[dict[str, JsonValue]] = []
    transport = StdioTransport(command, timeout=timeout)
    async with transport:
        for action, message in prepared:
            await transport.send(message)
            if "id" in message:
                pending.setdefault(_id_key(message["id"]), []).append(action.action_id)
                response_count += 1

        for _ in range(response_count):
            events.append(_normalize_response(await transport.receive(), pending))

    return ExecutionResult(tuple(events), transport.returncode, transport.stderr)


def _wire_message(action: Action) -> dict[str, JsonValue]:
    if action.kind is ActionKind.INITIALIZE:
        if action.protocol_version is None:
            raise ExecutionProtocolError("initialize requires a protocol version")
        params: JsonValue = (
            action.payload
            if action.payload is not None
            else {
                "capabilities": canonical_json(action.capabilities or {}),
                "clientInfo": {"name": "mcp-statecheck", "version": __version__},
                "protocolVersion": action.protocol_version,
            }
        )
        return {
            "id": canonical_json(action.mcp_request_id),
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": params,
        }
    if action.kind is ActionKind.REQUEST:
        if not action.method:
            raise ExecutionProtocolError("request requires a method")
        message: dict[str, JsonValue] = {
            "id": canonical_json(action.mcp_request_id),
            "jsonrpc": "2.0",
            "method": action.method,
        }
        if action.payload is not None:
            message["params"] = canonical_json(action.payload)
        return message
    raise ExecutionProtocolError(f"unsupported action kind: {action.kind.value}")


def _normalize_response(
    message: dict[str, object], pending: dict[str, list[str]]
) -> dict[str, JsonValue]:
    if (
        message.get("jsonrpc") != "2.0"
        or "method" in message
        or "id" not in message
        or (("result" in message) == ("error" in message))
    ):
        raise ExecutionProtocolError("peer emitted an invalid JSON-RPC response")
    if "error" in message:
        error = message["error"]
        if (
            not isinstance(error, Mapping)
            or not isinstance(error.get("code"), int)
            or isinstance(error.get("code"), bool)
            or not isinstance(error.get("message"), str)
        ):
            raise ExecutionProtocolError(
                "peer emitted an invalid JSON-RPC error object"
            )

    request_id = canonical_json(message["id"], where="response.id")
    candidates = pending.get(_id_key(request_id), [])
    if not candidates:
        raise ExecutionProtocolError("response id does not match a pending request")
    target_action_id = candidates[0] if len(candidates) == 1 else None
    if target_action_id is not None:
        pending.pop(_id_key(request_id), None)
    outcome = "success" if "result" in message else "error"
    payload = message["result"] if outcome == "success" else message["error"]
    return {
        "kind": "response",
        "mcp_request_id": request_id,
        "outcome": outcome,
        "payload": canonical_json(payload, where=f"response.{outcome}"),
        "target_action_id": target_action_id,
    }


def _id_key(value: object) -> str:
    return json.dumps(
        canonical_json(value, where="request id"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
