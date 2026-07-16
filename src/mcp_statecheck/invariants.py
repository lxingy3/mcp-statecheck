"""Normative checks over canonical actions and normalized observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

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
    actions: Sequence[Action], events: Sequence[Mapping[str, object]]
) -> Failure | None:
    """Return the first non-ping request sent before initialize replied."""

    if not all(isinstance(event, Mapping) for event in events):
        raise TypeError("events must contain objects")

    state = ModelState()
    initialize_action_id: str | None = None
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
        state = reduce(state, action)
        if action.kind is ActionKind.INITIALIZE:
            initialize_action_id = action.action_id
        elif action.kind in CONNECTION_BOUNDARIES:
            initialize_action_id = None
    return None


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
