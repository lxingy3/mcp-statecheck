"""Execute canonical actions at the wire boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from . import __version__
from .model import Action, ActionKind, JsonValue, canonical_json
from .transports import (
    HTTPStatusError,
    HTTPTimeout,
    StdioTransport,
    StreamableHTTPTransport,
)


class ExecutionProtocolError(Exception):
    """A canonical action or peer response cannot be executed safely."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    events: tuple[dict[str, JsonValue], ...]
    returncode: int | None
    stderr: str
    cleanup: dict[str, JsonValue] = field(default_factory=dict)


async def execute_stdio(
    actions: Sequence[Action], command: Sequence[str], *, timeout: float = 5.0
) -> ExecutionResult:
    """Execute initialize and request actions against one stdio peer."""

    prepared: list[tuple[Action, dict[str, JsonValue] | None]] = []
    action_ids: set[str] = set()
    for action in actions:
        if not isinstance(action, Action):
            raise TypeError("actions must contain Action values")
        if action.action_id in action_ids:
            raise ExecutionProtocolError(
                f"duplicate internal action_id: {action.action_id}"
            )
        action_ids.add(action.action_id)
        message = None if action.kind is ActionKind.RESPONSE else _wire_message(action)
        prepared.append((action, message))

    pending: dict[str, list[str]] = {}
    remaining_response_count = 0
    events: list[dict[str, JsonValue]] = []
    transport = StdioTransport(command, timeout=timeout)
    async with transport:
        for action, message in prepared:
            if action.kind is ActionKind.RESPONSE:
                if remaining_response_count == 0:
                    raise ExecutionProtocolError(
                        "response barrier has no pending request"
                    )
                event = _normalize_response(await transport.receive(), pending)
                if (
                    action.target_action_id is not None
                    and event["target_action_id"] != action.target_action_id
                ):
                    raise ExecutionProtocolError(
                        "response barrier received a different request"
                    )
                events.append(event)
                remaining_response_count -= 1
                continue
            if message is None:
                raise AssertionError("outbound action requires a wire message")
            await transport.send(message)
            if "id" in message:
                pending.setdefault(_id_key(message["id"]), []).append(action.action_id)
                remaining_response_count += 1

        for _ in range(remaining_response_count):
            events.append(_normalize_response(await transport.receive(), pending))

    return ExecutionResult(tuple(events), transport.returncode, transport.stderr)


async def execute_http(
    actions: Sequence[Action], url: str, *, timeout: float = 5.0
) -> ExecutionResult:
    """Execute canonical actions against one Streamable HTTP endpoint."""

    action_ids: set[str] = set()
    supported = {
        ActionKind.CONNECT,
        ActionKind.INITIALIZE,
        ActionKind.INITIALIZED,
        ActionKind.OPEN_STREAM,
        ActionKind.RESUME_STREAM,
    }
    for action in actions:
        if not isinstance(action, Action):
            raise TypeError("actions must contain Action values")
        if action.action_id in action_ids:
            raise ExecutionProtocolError(
                f"duplicate internal action_id: {action.action_id}"
            )
        if action.kind not in supported:
            raise ExecutionProtocolError(
                f"unsupported HTTP action kind: {action.kind.value}"
            )
        action_ids.add(action.action_id)

    events: list[dict[str, JsonValue]] = []
    transport = StreamableHTTPTransport(url, timeout=timeout)
    async with transport:
        for action in actions:
            if action.kind is ActionKind.CONNECT:
                continue
            if action.kind in (ActionKind.INITIALIZE, ActionKind.INITIALIZED):
                message = _wire_message(action)
                try:
                    messages = await transport.send(message)
                except HTTPStatusError as exc:
                    events.append(_http_status_event(action, "POST", exc.status_code))
                    break
                except HTTPTimeout as exc:
                    events.append(_http_timeout_event(action, "POST", exc.status_code))
                    break
                if action.kind is ActionKind.INITIALIZE:
                    pending = {_id_key(action.mcp_request_id): [action.action_id]}
                    normalized = [
                        _normalize_response(message, pending) for message in messages
                    ]
                    events.extend(normalized)
                    if normalized:
                        if normalized[-1]["outcome"] != "success":
                            break
                        payload = normalized[-1]["payload"]
                        if isinstance(payload, Mapping):
                            version = payload.get("protocolVersion")
                            if isinstance(version, str):
                                transport.set_protocol_version(version)
                continue

            if not action.stream_id:
                raise ExecutionProtocolError("stream action requires a stream_id")
            cursor = transport.cursor(action.stream_id)
            if action.kind is ActionKind.OPEN_STREAM:
                if action.resume_token is not None:
                    raise ExecutionProtocolError(
                        "open_stream cannot include a resume token"
                    )
                sent_last_event_id = cursor
                override = None
            else:
                override = action.resume_token
                sent_last_event_id = cursor if override is None else override or None
            try:
                messages = await transport.resume(
                    action.stream_id,
                    last_event_id=override,
                )
            except HTTPStatusError as exc:
                events.append(_http_status_event(action, "GET", exc.status_code))
                break
            except HTTPTimeout as exc:
                events.append(_http_timeout_event(action, "GET", exc.status_code))
                break
            events.append(
                {
                    "kind": "sse_resume",
                    "payload": canonical_json(messages[0]) if messages else None,
                    "received_event_id": transport.cursor(action.stream_id),
                    "sent_last_event_id": sent_last_event_id,
                    "session_id": transport.session_id,
                    "stream_id": action.stream_id,
                    "target_action_id": action.action_id,
                }
            )

    return ExecutionResult(
        tuple(events),
        None,
        "",
        cleanup={"client_closed": True},
    )


def _http_status_event(
    action: Action, method: str, status: int
) -> dict[str, JsonValue]:
    return {
        "http_method": method,
        "kind": "http_error",
        "status": status,
        "target_action_id": action.action_id,
    }


def _http_timeout_event(
    action: Action, method: str, status: int | None
) -> dict[str, JsonValue]:
    return {
        "http_method": method,
        "kind": "timeout",
        "status": status,
        "target_action_id": action.action_id,
    }


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
    if action.kind is ActionKind.INITIALIZED:
        return {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
    if action.kind is ActionKind.CANCEL:
        return {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": canonical_json(action.mcp_request_id)},
        }
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
