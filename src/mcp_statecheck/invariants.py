"""Normative checks over canonical actions and normalized observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .fixtures import HTTP_ERROR_FIXTURE_ID, SSE_RESUME_FIXTURE_ID
from .model import (
    Action,
    ActionKind,
    JsonValue,
    ModelState,
    canonical_json,
    reduce,
)

INITIALIZATION_SPEC = (
    "https://modelcontextprotocol.io/specification/2025-11-25/"
    "basic/lifecycle#initialization"
)
EARLY_REQUEST_KIND = "lifecycle.client_request_before_initialize_response"
DUPLICATE_REQUEST_ID_SPEC = (
    "https://modelcontextprotocol.io/specification/2025-11-25/basic/index#requests"
)
DUPLICATE_REQUEST_ID_KIND = "messages.request_id_reused_within_session"
RESPONSE_CORRELATION_SPEC = (
    "https://modelcontextprotocol.io/specification/2025-11-25/basic/index#responses"
)
LATE_RESPONSE_FIXTURE_ID = "late-response-after-cancellation"
WRONG_RESPONSE_CORRELATION_KIND = "differential.response_correlated_to_wrong_request"
HTTP_TRANSPORT_SPEC = (
    "https://modelcontextprotocol.io/specification/2025-11-25/"
    "basic/transports#sending-messages-to-the-server"
)
HTTP_STATUS_TIMEOUT_KIND = "differential.http_status_reported_as_timeout"
SSE_RESUME_SPEC = (
    "https://modelcontextprotocol.io/specification/2025-11-25/"
    "basic/transports#resumability-and-redelivery"
)
SSE_RESUME_TOKEN_LOST_KIND = "differential.sse_resume_token_lost"
CONNECTION_BOUNDARIES = {
    ActionKind.CONNECT,
    ActionKind.RECONNECT,
    ActionKind.SESSION_EXPIRED,
    ActionKind.DISCONNECT,
    ActionKind.CLOSE,
}


@dataclass(frozen=True, slots=True)
class Failure:
    """One stable, spec-linked protocol failure."""

    kind: str
    spec_reference: str
    trigger_action_id: str
    evidence: dict[str, JsonValue]
    signature: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "evidence": canonical_json(self.evidence),
            "kind": self.kind,
            "signature": self.signature,
            "spec_reference": self.spec_reference,
            "trigger_action_id": self.trigger_action_id,
        }


def detect_failure(
    actions: Sequence[Action],
    events: Sequence[Mapping[str, object]],
    *,
    fixture_id: str | None = None,
) -> Failure | None:
    """Return the first stable failure in canonical actions and observations."""

    if not all(isinstance(event, Mapping) for event in events):
        raise TypeError("events must contain objects")

    state = ModelState()
    initialize_action_id: str | None = None
    session_active = False
    used_request_ids: dict[str, tuple[str, str | None]] = {}
    for action in actions:
        if not isinstance(action, Action):
            raise TypeError("actions must contain Action values")
        if action.kind is ActionKind.REQUEST and action.method != "ping":
            initialize = (
                state.request(initialize_action_id)
                if initialize_action_id is not None
                else None
            )
            if initialize is not None and not initialize.response_action_ids:
                evidence = {
                    "direction": "client_to_server",
                    "lifecycle_boundary": "awaiting_initialize_response",
                    "method": action.method,
                    "subject": "client",
                }
                return Failure(
                    kind=EARLY_REQUEST_KIND,
                    spec_reference=INITIALIZATION_SPEC,
                    trigger_action_id=action.action_id,
                    evidence=evidence,
                    signature=_signature(EARLY_REQUEST_KIND, evidence),
                )
        if action.kind is ActionKind.INITIALIZE:
            session_active = True
        if (
            session_active
            and action.kind
            in (
                ActionKind.INITIALIZE,
                ActionKind.REQUEST,
            )
            and (
                isinstance(action.mcp_request_id, str)
                or type(action.mcp_request_id) is int
            )
        ):
            request_id_key = json.dumps(
                action.mcp_request_id,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            method = (
                "initialize" if action.kind is ActionKind.INITIALIZE else action.method
            )
            previous = used_request_ids.get(request_id_key)
            if previous is not None:
                previous_action_id, previous_method = previous
                previous_request = state.request(previous_action_id)
                overlap = (
                    previous_request.status.value
                    if previous_request is not None
                    else "unknown"
                )
                evidence = {
                    "direction": "client_to_server",
                    "mcp_request_id": action.mcp_request_id,
                    "method": method,
                    "overlap": overlap,
                    "previous_action_id": previous_action_id,
                    "previous_method": previous_method,
                    "session_scope": "same_session",
                    "subject": "client",
                }
                signature_evidence = {
                    "direction": "client_to_server",
                    "overlap": overlap,
                    "session_scope": "same_session",
                    "subject": "client",
                }
                return Failure(
                    kind=DUPLICATE_REQUEST_ID_KIND,
                    spec_reference=DUPLICATE_REQUEST_ID_SPEC,
                    trigger_action_id=action.action_id,
                    evidence=evidence,
                    signature=_signature(
                        DUPLICATE_REQUEST_ID_KIND,
                        signature_evidence,
                    ),
                )
            used_request_ids[request_id_key] = (action.action_id, method)
        state = reduce(state, action)
        if action.kind is ActionKind.INITIALIZE:
            initialize_action_id = action.action_id
        elif action.kind in CONNECTION_BOUNDARIES:
            initialize_action_id = None
            session_active = False
            used_request_ids.clear()
    if fixture_id == HTTP_ERROR_FIXTURE_ID:
        return _detect_http_status_as_timeout(actions, events)
    if fixture_id == SSE_RESUME_FIXTURE_ID:
        return _detect_sse_resume_token_loss(actions, events)
    if fixture_id == LATE_RESPONSE_FIXTURE_ID:
        return _detect_fixture_canary_failure(actions, events)
    return None


def _detect_http_status_as_timeout(
    actions: Sequence[Action], events: Sequence[Mapping[str, object]]
) -> Failure | None:
    actions_by_id = {action.action_id: action for action in actions}
    positions = {action.action_id: index for index, action in enumerate(actions)}
    for event in events:
        target_action_id = event.get("target_action_id")
        target = (
            actions_by_id.get(target_action_id)
            if isinstance(target_action_id, str)
            else None
        )
        status = event.get("status")
        if (
            target is None
            or target.kind is not ActionKind.INITIALIZE
            or event.get("kind") != "timeout"
            or event.get("fixture_source_kind") != "http_error"
            or event.get("http_method") != "POST"
            or type(status) is not int
            or not 500 <= status <= 599
        ):
            continue
        target_position = positions[target.action_id]
        boundary_positions = [
            index
            for index, action in enumerate(actions[:target_position])
            if action.kind in CONNECTION_BOUNDARIES
        ]
        if (
            not boundary_positions
            or actions[boundary_positions[-1]].kind is not ActionKind.CONNECT
            or any(
                action.kind is ActionKind.INITIALIZE
                for action in actions[boundary_positions[-1] + 1 : target_position]
            )
        ):
            continue
        signature_evidence = {
            "direction": "server_to_client",
            "http_method": "POST",
            "oracle": "fixture_http_status",
            "reported_kind": "timeout",
            "source_kind": "http_error",
            "status_class": "5xx",
            "subject": "client_adapter",
        }
        evidence: dict[str, JsonValue] = {
            **signature_evidence,
            "http_status": status,
            "target_action_id": target.action_id,
        }
        return Failure(
            kind=HTTP_STATUS_TIMEOUT_KIND,
            spec_reference=HTTP_TRANSPORT_SPEC,
            trigger_action_id=target.action_id,
            evidence=evidence,
            signature=_signature(HTTP_STATUS_TIMEOUT_KIND, signature_evidence),
        )
    return None


def _detect_sse_resume_token_loss(
    actions: Sequence[Action], events: Sequence[Mapping[str, object]]
) -> Failure | None:
    required_fields = {
        "peer_last_event_id",
        "peer_protocol_version",
        "peer_session_id",
        "received_event_id",
        "sent_last_event_id",
        "session_id",
        "stream_id",
        "target_action_id",
    }
    positions = {action.action_id: index for index, action in enumerate(actions)}
    observations_by_target: dict[str, list[Mapping[str, object]]] = {}
    for event in events:
        target_action_id = event.get("target_action_id")
        if event.get("kind") == "sse_resume" and isinstance(target_action_id, str):
            observations_by_target.setdefault(target_action_id, []).append(event)

    for opened in actions:
        if opened.kind is not ActionKind.OPEN_STREAM or not opened.stream_id:
            continue
        resumes = [
            action
            for action in actions[positions[opened.action_id] + 1 :]
            if action.kind is ActionKind.RESUME_STREAM
            and action.stream_id == opened.stream_id
        ]
        for first_resume, second_resume in zip(resumes, resumes[1:], strict=False):
            if (
                not first_resume.resume_token
                or not second_resume.resume_token
                or first_resume.resume_token == second_resume.resume_token
            ):
                continue
            event_groups = [
                observations_by_target.get(action.action_id, [])
                for action in (opened, first_resume, second_resume)
            ]
            if any(len(group) != 1 for group in event_groups):
                continue
            observations = tuple(group[0] for group in event_groups)
            if any(not required_fields <= event.keys() for event in observations):
                continue

            initialize_positions = [
                index
                for index, action in enumerate(actions[: positions[opened.action_id]])
                if action.kind is ActionKind.INITIALIZE
            ]
            if not initialize_positions:
                continue
            initialize_position = initialize_positions[-1]
            initialize_action = actions[initialize_position]
            if not any(
                event.get("kind") == "response"
                and event.get("target_action_id") == initialize_action.action_id
                and event.get("outcome") == "success"
                for event in events
            ):
                continue
            between_initialize_and_open = actions[
                initialize_position + 1 : positions[opened.action_id]
            ]
            if not any(
                action.kind is ActionKind.INITIALIZED
                for action in between_initialize_and_open
            ) or any(
                action.kind in CONNECTION_BOUNDARIES
                or action.kind is ActionKind.INITIALIZE
                for action in actions[
                    initialize_position + 1 : positions[second_resume.action_id]
                ]
            ):
                continue

            open_event, first_event, second_event = observations
            peer_session = open_event["peer_session_id"]
            protocol_version = initialize_action.protocol_version
            if (
                any(event["stream_id"] != opened.stream_id for event in observations)
                or not isinstance(peer_session, str)
                or not peer_session
                or any(
                    event["peer_session_id"] != peer_session for event in observations
                )
                or not protocol_version
                or any(
                    event["peer_protocol_version"] != protocol_version
                    for event in observations
                )
                or any(
                    event["sent_last_event_id"] is not None
                    and not isinstance(event["sent_last_event_id"], str)
                    for event in observations
                )
                or open_event["peer_last_event_id"] is not None
                or open_event["received_event_id"] != first_resume.resume_token
                or first_event["peer_last_event_id"] != first_resume.resume_token
                or first_event["received_event_id"] != second_resume.resume_token
                or second_event["peer_last_event_id"] is not None
                or not isinstance(second_event["received_event_id"], str)
                or not second_event["received_event_id"]
            ):
                continue

            signature_evidence = {
                "direction": "client_to_server",
                "last_event_id_state": "missing",
                "oracle": "fixture_sse_header",
                "reconnect": "subsequent",
                "session_scope": "same_session",
                "stream_scope": "same_stream",
                "subject": "client_adapter",
                "transport": "streamable_http",
            }
            evidence: dict[str, JsonValue] = {
                **signature_evidence,
                "expected_last_event_id": second_resume.resume_token,
                "observed_last_event_id": None,
                "reported_last_event_id": canonical_json(
                    second_event["sent_last_event_id"],
                    where="event.sent_last_event_id",
                ),
                "resume_ordinal": 2,
                "stream_id": opened.stream_id,
                "target_action_id": second_resume.action_id,
            }
            return Failure(
                kind=SSE_RESUME_TOKEN_LOST_KIND,
                spec_reference=SSE_RESUME_SPEC,
                trigger_action_id=second_resume.action_id,
                evidence=evidence,
                signature=_signature(SSE_RESUME_TOKEN_LOST_KIND, signature_evidence),
            )
    return None


def _detect_fixture_canary_failure(
    actions: Sequence[Action], events: Sequence[Mapping[str, object]]
) -> Failure | None:
    positions = {action.action_id: index for index, action in enumerate(actions)}
    actions_by_id = {action.action_id: action for action in actions}
    cancellations = {
        action.target_action_id: (action, positions[action.action_id])
        for action in actions
        if action.kind is ActionKind.CANCEL and action.target_action_id in actions_by_id
    }
    canary_actions: dict[str, Action | None] = {}
    for action in actions:
        canary = _request_canary(action)
        if canary is not None:
            canary_actions[canary] = None if canary in canary_actions else action

    barrier_positions = iter(
        index
        for index, action in enumerate(actions)
        if action.kind is ActionKind.RESPONSE
    )
    mismatches: dict[tuple[str, str], tuple[Mapping[str, object], int]] = {}
    for event in events:
        event_position = next(barrier_positions, len(actions))
        target_action_id = event.get("target_action_id")
        source = canary_actions.get(_response_canary(event))
        target = (
            actions_by_id.get(target_action_id)
            if isinstance(target_action_id, str)
            else None
        )
        if (
            source is not None
            and target is not None
            and source.action_id != target.action_id
            and event.get("mcp_request_id") == target.mcp_request_id
        ):
            mismatches[(source.action_id, target.action_id)] = (
                event,
                event_position,
            )

    for (source_action_id, target_action_id), (
        event,
        event_position,
    ) in mismatches.items():
        cancellation = cancellations.get(source_action_id)
        reciprocal = mismatches.get((target_action_id, source_action_id))
        source = actions_by_id[source_action_id]
        target = actions_by_id[target_action_id]
        if (
            cancellation is None
            or reciprocal is None
            or reciprocal[0].get("mcp_request_id") != source.mcp_request_id
        ):
            continue
        cancel_action, cancel_position = cancellation
        if cancel_action.mcp_request_id != source.mcp_request_id:
            continue
        if not (
            positions[source_action_id]
            < cancel_position
            < positions[target_action_id]
            < min(event_position, reciprocal[1])
        ):
            continue
        signature_evidence = {
            "cancellation_context": "after_cancellation",
            "correlation": "wrong_request",
            "direction": "server_to_client",
            "oracle": "fixture_canary",
            "subject": "server",
        }
        evidence: dict[str, JsonValue] = {
            **signature_evidence,
            "expected_canary": _request_canary(target),
            "observed_canary": _response_canary(event),
            "response_mcp_request_id": canonical_json(
                event.get("mcp_request_id"),
                where="event.mcp_request_id",
            ),
            "source_action_id": source_action_id,
            "source_request_status": "cancelled",
            "target_action_id": target_action_id,
        }
        return Failure(
            kind=WRONG_RESPONSE_CORRELATION_KIND,
            spec_reference=RESPONSE_CORRELATION_SPEC,
            trigger_action_id=target_action_id,
            evidence=evidence,
            signature=_signature(
                WRONG_RESPONSE_CORRELATION_KIND,
                signature_evidence,
            ),
        )
    return None


def _request_canary(action: Action) -> str | None:
    if action.kind is not ActionKind.REQUEST or not isinstance(action.payload, Mapping):
        return None
    arguments = action.payload.get("arguments")
    if not isinstance(arguments, Mapping):
        return None
    canary = arguments.get("fixtureCanary")
    return canary if isinstance(canary, str) else None


def _response_canary(event: Mapping[str, object]) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    structured = payload.get("structuredContent")
    if not isinstance(structured, Mapping):
        return None
    canary = structured.get("fixtureCanary")
    return canary if isinstance(canary, str) else None


def _signature(kind: str, evidence: Mapping[str, object]) -> str:
    payload = canonical_json(
        {
            "evidence": dict(evidence),
            "kind": kind,
            "signature_schema": 1,
        }
    )
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"mcp-statecheck:v1:{hashlib.sha256(encoded).hexdigest()}"
