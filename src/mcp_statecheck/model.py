"""Canonical protocol actions and lifecycle reducer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from math import isfinite
from typing import Self

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
PROTOCOL_VERSIONS = ("2025-06-18", "2025-11-25")


def _validate_json_string(value: str, where: str) -> str:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise TypeError(f"{where} contains an unpaired Unicode surrogate")
    return value


def canonical_json(value: object, *, where: str = "value") -> JsonValue:
    """Return a detached, deterministically ordered JSON value."""
    if isinstance(value, str):
        return _validate_json_string(value, where)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise TypeError(f"{where} contains a non-finite number")
        return value
    if isinstance(value, list):
        return [
            canonical_json(item, where=f"{where}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError(f"{where} contains a non-string object key")
        return {
            _validate_json_string(key, f"{where} key"): canonical_json(
                value[key], where=f"{where}.{key}"
            )
            for key in sorted(value)
        }
    raise TypeError(f"{where} is not JSON serializable: {type(value).__name__}")


def is_valid_initialize_result(value: object) -> bool:
    """Return whether a value is a supported MCP InitializeResult."""
    if not isinstance(value, Mapping):
        return False
    server_info = value.get("serverInfo")
    return (
        value.get("protocolVersion") in PROTOCOL_VERSIONS
        and isinstance(value.get("capabilities"), Mapping)
        and isinstance(server_info, Mapping)
        and isinstance(server_info.get("name"), str)
        and isinstance(server_info.get("version"), str)
    )


class LifecyclePhase(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    CLOSED = "closed"


class ActionKind(StrEnum):
    CONNECT = "connect"
    RECONNECT = "reconnect"
    INITIALIZE = "initialize"
    INITIALIZED = "initialized"
    REQUEST = "request"
    NOTIFICATION = "notification"
    RESPONSE = "response"
    CANCEL = "cancel"
    OPEN_STREAM = "open_stream"
    DISCONNECT_STREAM = "disconnect_stream"
    RESUME_STREAM = "resume_stream"
    SESSION_EXPIRED = "session_expired"
    DISCONNECT = "disconnect"
    CLOSE = "close"


class RequestStatus(StrEnum):
    PENDING = "pending"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    LATE_RESPONSE = "late_response"


@dataclass(frozen=True, slots=True)
class Action:
    """One canonical action, including protocol-invalid but serializable sends."""

    action_id: str
    kind: ActionKind
    mcp_request_id: JsonValue = None
    target_action_id: str | None = None
    method: str | None = None
    payload: JsonValue = None
    protocol_version: str | None = None
    capabilities: dict[str, JsonValue] | None = None
    stream_id: str | None = None
    resume_token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id:
            raise ValueError("action_id must be a non-empty string")
        if not isinstance(self.kind, ActionKind):
            object.__setattr__(self, "kind", ActionKind(self.kind))
        if self.target_action_id is not None and not isinstance(
            self.target_action_id, str
        ):
            raise TypeError("target_action_id must be a string or null")
        if self.method is not None and not isinstance(self.method, str):
            raise TypeError("method must be a string or null")
        if self.protocol_version is not None and not isinstance(
            self.protocol_version, str
        ):
            raise TypeError("protocol_version must be a string or null")
        if self.stream_id is not None and not isinstance(self.stream_id, str):
            raise TypeError("stream_id must be a string or null")
        if self.resume_token is not None and not isinstance(self.resume_token, str):
            raise TypeError("resume_token must be a string or null")
        object.__setattr__(
            self,
            "mcp_request_id",
            canonical_json(self.mcp_request_id, where="mcp_request_id"),
        )
        object.__setattr__(
            self, "payload", canonical_json(self.payload, where="payload")
        )
        if self.capabilities is not None:
            capabilities = canonical_json(self.capabilities, where="capabilities")
            if not isinstance(capabilities, dict):
                raise TypeError("capabilities must be an object or null")
            object.__setattr__(self, "capabilities", capabilities)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "action_id": self.action_id,
            "capabilities": canonical_json(self.capabilities),
            "kind": self.kind.value,
            "mcp_request_id": canonical_json(self.mcp_request_id),
            "method": self.method,
            "payload": canonical_json(self.payload),
            "protocol_version": self.protocol_version,
            "resume_token": self.resume_token,
            "stream_id": self.stream_id,
            "target_action_id": self.target_action_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        _check_keys(
            data,
            required={"action_id", "kind"},
            optional={
                "capabilities",
                "mcp_request_id",
                "method",
                "payload",
                "protocol_version",
                "resume_token",
                "stream_id",
                "target_action_id",
            },
            where="action",
        )
        capabilities = data.get("capabilities")
        if capabilities is not None and not isinstance(capabilities, Mapping):
            raise TypeError("action.capabilities must be an object or null")
        return cls(
            action_id=data["action_id"],  # type: ignore[arg-type]
            kind=ActionKind(data["kind"]),  # type: ignore[arg-type]
            mcp_request_id=data.get("mcp_request_id"),  # type: ignore[arg-type]
            target_action_id=data.get("target_action_id"),  # type: ignore[arg-type]
            method=data.get("method"),  # type: ignore[arg-type]
            payload=data.get("payload"),  # type: ignore[arg-type]
            protocol_version=data.get("protocol_version"),  # type: ignore[arg-type]
            capabilities=dict(capabilities) if capabilities is not None else None,
            stream_id=data.get("stream_id"),  # type: ignore[arg-type]
            resume_token=data.get("resume_token"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class RequestRecord:
    action_id: str
    mcp_request_id: JsonValue
    method: str | None
    status: RequestStatus = RequestStatus.PENDING
    cancel_action_ids: tuple[str, ...] = ()
    response_action_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "action_id": self.action_id,
            "cancel_action_ids": list(self.cancel_action_ids),
            "mcp_request_id": canonical_json(self.mcp_request_id),
            "method": self.method,
            "response_action_ids": list(self.response_action_ids),
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        _check_keys(
            data,
            required={"action_id", "mcp_request_id", "method", "status"},
            optional={"cancel_action_ids", "response_action_ids"},
            where="request",
        )
        return cls(
            action_id=_string(data["action_id"], "request.action_id"),
            mcp_request_id=canonical_json(
                data["mcp_request_id"], where="request.mcp_request_id"
            ),
            method=_optional_string(data["method"], "request.method"),
            status=RequestStatus(data["status"]),  # type: ignore[arg-type]
            cancel_action_ids=_string_tuple(
                data.get("cancel_action_ids", []), "request.cancel_action_ids"
            ),
            response_action_ids=_string_tuple(
                data.get("response_action_ids", []), "request.response_action_ids"
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelState:
    phase: LifecyclePhase = LifecyclePhase.DISCONNECTED
    requested_protocol_version: str | None = None
    negotiated_protocol_version: str | None = None
    client_capabilities: dict[str, JsonValue] | None = None
    server_capabilities: dict[str, JsonValue] | None = None
    actions: tuple[Action, ...] = ()
    requests: tuple[RequestRecord, ...] = ()
    unmatched_response_action_ids: tuple[str, ...] = ()
    unmatched_cancel_action_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("client_capabilities", "server_capabilities"):
            value = getattr(self, name)
            if value is not None:
                canonical = canonical_json(value, where=name)
                if not isinstance(canonical, dict):
                    raise TypeError(f"{name} must be an object or null")
                object.__setattr__(self, name, canonical)

    def request(self, action_id: str) -> RequestRecord | None:
        return next(
            (request for request in self.requests if request.action_id == action_id),
            None,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "actions": [action.to_dict() for action in self.actions],
            "client_capabilities": canonical_json(self.client_capabilities),
            "negotiated_protocol_version": self.negotiated_protocol_version,
            "phase": self.phase.value,
            "requested_protocol_version": self.requested_protocol_version,
            "requests": [request.to_dict() for request in self.requests],
            "server_capabilities": canonical_json(self.server_capabilities),
            "unmatched_cancel_action_ids": list(self.unmatched_cancel_action_ids),
            "unmatched_response_action_ids": list(self.unmatched_response_action_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        _check_keys(
            data,
            required={"phase"},
            optional={
                "actions",
                "client_capabilities",
                "negotiated_protocol_version",
                "requested_protocol_version",
                "requests",
                "server_capabilities",
                "unmatched_cancel_action_ids",
                "unmatched_response_action_ids",
            },
            where="model state",
        )
        actions = _mapping_list(data.get("actions", []), "model state.actions")
        requests = _mapping_list(data.get("requests", []), "model state.requests")
        client = data.get("client_capabilities")
        server = data.get("server_capabilities")
        if client is not None and not isinstance(client, Mapping):
            raise TypeError("model state.client_capabilities must be an object or null")
        if server is not None and not isinstance(server, Mapping):
            raise TypeError("model state.server_capabilities must be an object or null")
        return cls(
            phase=LifecyclePhase(data["phase"]),  # type: ignore[arg-type]
            requested_protocol_version=_optional_string(
                data.get("requested_protocol_version"),
                "model state.requested_protocol_version",
            ),
            negotiated_protocol_version=_optional_string(
                data.get("negotiated_protocol_version"),
                "model state.negotiated_protocol_version",
            ),
            client_capabilities=dict(client) if client is not None else None,
            server_capabilities=dict(server) if server is not None else None,
            actions=tuple(Action.from_dict(item) for item in actions),
            requests=tuple(RequestRecord.from_dict(item) for item in requests),
            unmatched_response_action_ids=_string_tuple(
                data.get("unmatched_response_action_ids", []),
                "model state.unmatched_response_action_ids",
            ),
            unmatched_cancel_action_ids=_string_tuple(
                data.get("unmatched_cancel_action_ids", []),
                "model state.unmatched_cancel_action_ids",
            ),
        )


def reduce(state: ModelState, action: Action) -> ModelState:
    """Apply one action without enforcing MCP conformance rules."""
    if any(existing.action_id == action.action_id for existing in state.actions):
        raise ValueError(f"duplicate internal action_id: {action.action_id}")

    state = replace(state, actions=state.actions + (action,))
    if action.kind in (ActionKind.CONNECT, ActionKind.RECONNECT):
        return _clear_negotiation(state, LifecyclePhase.CONNECTED)
    if action.kind is ActionKind.INITIALIZE:
        state = _append_request(state, action, method="initialize")
        return replace(
            state,
            phase=LifecyclePhase.INITIALIZING,
            requested_protocol_version=action.protocol_version,
            client_capabilities=action.capabilities,
            negotiated_protocol_version=None,
            server_capabilities=None,
        )
    if action.kind is ActionKind.INITIALIZED:
        return replace(state, phase=LifecyclePhase.INITIALIZED)
    if action.kind is ActionKind.REQUEST:
        return _append_request(state, action, method=action.method)
    if action.kind is ActionKind.RESPONSE:
        return _apply_response(state, action)
    if action.kind is ActionKind.CANCEL:
        return _apply_cancel(state, action)
    if action.kind is ActionKind.SESSION_EXPIRED:
        return _clear_negotiation(state, LifecyclePhase.CONNECTED)
    if action.kind is ActionKind.DISCONNECT:
        return _clear_negotiation(state, LifecyclePhase.DISCONNECTED)
    if action.kind is ActionKind.CLOSE:
        return replace(state, phase=LifecyclePhase.CLOSED)
    return state


def _clear_negotiation(state: ModelState, phase: LifecyclePhase) -> ModelState:
    return replace(
        state,
        phase=phase,
        requested_protocol_version=None,
        negotiated_protocol_version=None,
        client_capabilities=None,
        server_capabilities=None,
    )


def _append_request(
    state: ModelState, action: Action, *, method: str | None
) -> ModelState:
    return replace(
        state,
        requests=state.requests
        + (RequestRecord(action.action_id, action.mcp_request_id, method),),
    )


def _apply_response(state: ModelState, action: Action) -> ModelState:
    index = _target_request_index(state, action)
    if index is None:
        return replace(
            state,
            unmatched_response_action_ids=state.unmatched_response_action_ids
            + (action.action_id,),
        )

    request = state.requests[index]
    status = request.status
    if status in (RequestStatus.CANCELLED, RequestStatus.LATE_RESPONSE):
        status = RequestStatus.LATE_RESPONSE
    elif status is RequestStatus.PENDING:
        status = RequestStatus.COMPLETED
    updated = replace(
        request,
        status=status,
        response_action_ids=request.response_action_ids + (action.action_id,),
    )
    requests = list(state.requests)
    requests[index] = updated
    changes: dict[str, object] = {"requests": tuple(requests)}
    if request.method == "initialize":
        if action.protocol_version is not None:
            changes["negotiated_protocol_version"] = action.protocol_version
        if action.capabilities is not None:
            changes["server_capabilities"] = action.capabilities
    return replace(state, **changes)


def _apply_cancel(state: ModelState, action: Action) -> ModelState:
    index = _target_request_index(state, action)
    if index is None:
        return replace(
            state,
            unmatched_cancel_action_ids=state.unmatched_cancel_action_ids
            + (action.action_id,),
        )

    request = state.requests[index]
    status = (
        RequestStatus.CANCELLED
        if request.status is RequestStatus.PENDING
        else request.status
    )
    updated = replace(
        request,
        status=status,
        cancel_action_ids=request.cancel_action_ids + (action.action_id,),
    )
    requests = list(state.requests)
    requests[index] = updated
    return replace(state, requests=tuple(requests))


def _target_request_index(state: ModelState, action: Action) -> int | None:
    if action.target_action_id is None:
        return None
    return next(
        (
            index
            for index, request in enumerate(state.requests)
            if request.action_id == action.target_action_id
        ),
        None,
    )


def _check_keys(
    data: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str],
    where: str,
) -> None:
    if not isinstance(data, Mapping):
        raise TypeError(f"{where} must be an object")
    missing = required - data.keys()
    unknown = data.keys() - required - optional
    if missing:
        raise ValueError(f"{where} missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{where} has unknown field(s): {', '.join(sorted(unknown))}")


def _string(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{where} must be a string")
    return value


def _optional_string(value: object, where: str) -> str | None:
    return None if value is None else _string(value, where)


def _string_tuple(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{where} must be an array of strings")
    return tuple(value)


def _mapping_list(value: object, where: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        raise TypeError(f"{where} must be an array of objects")
    return value
