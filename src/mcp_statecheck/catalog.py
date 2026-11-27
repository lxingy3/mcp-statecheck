"""Compare the tool catalogs recorded by successful quick checks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from .model import MODERN_PROTOCOL_VERSIONS, PROTOCOL_VERSIONS
from .reports import Artifact, ReportError

SCHEMA_FIELDS = ("inputSchema", "outputSchema")


def tool_list_problem(payload: object) -> str | None:
    tools = payload.get("tools") if isinstance(payload, Mapping) else None
    if not isinstance(tools, list):
        return "invalid_tools_array"
    for tool in tools:
        if (
            not isinstance(tool, Mapping)
            or not isinstance(tool.get("name"), str)
            or not tool.get("name")
            or not isinstance(tool.get("inputSchema"), Mapping)
        ):
            return "invalid_tool_definition"
        if "outputSchema" in tool and not isinstance(tool["outputSchema"], Mapping):
            return "invalid_output_schema"
    if "nextCursor" in payload and not isinstance(payload["nextCursor"], str):
        return "invalid_next_cursor"
    return None


@dataclass(frozen=True)
class _Catalog:
    protocol_version: str
    transport: str
    tools_declared: bool
    schemas: dict[str, dict[str, str]]


def _read_catalog(artifact: Artifact) -> _Catalog:
    generation = artifact.get("generation")
    if (
        artifact.get("fixture_id") != "server-smoke"
        or artifact.get("adapter") != "wire"
        or "failure" in artifact
        or not isinstance(generation, dict)
        or generation.get("profile") != "quick"
        or generation.get("outcome") != "passed"
    ):
        raise ReportError("expected a successful check artifact")

    version = artifact["protocol_version"]
    transport = artifact["transport"]
    if version not in (*PROTOCOL_VERSIONS, *MODERN_PROTOCOL_VERSIONS):
        raise ReportError("unsupported check protocol version")
    if transport not in ("stdio", "streamable-http"):
        raise ReportError("unsupported check transport")
    modern = version in MODERN_PROTOCOL_VERSIONS
    entry = "discover" if modern else "initialize"
    responses = [
        event
        for event in artifact["normalized_events"]
        if event.get("kind") == "response"
    ]
    handshake = [event for event in responses if event.get("target_action_id") == entry]
    if len(handshake) != 1 or handshake[0].get("outcome") != "success":
        raise ReportError(f"expected one successful {entry} response")
    response = handshake[0].get("payload")
    capabilities = response.get("capabilities") if isinstance(response, dict) else None
    if not isinstance(capabilities, dict):
        raise ReportError("check artifact does not contain server capabilities")
    if modern:
        supported = response.get("supportedVersions")
        if (
            response.get("resultType") != "complete"
            or not isinstance(supported, list)
            or version not in supported
        ):
            raise ReportError("check artifact does not confirm the requested protocol")
    else:
        version = response.get("protocolVersion")
        if version not in PROTOCOL_VERSIONS:
            raise ReportError(
                "check artifact does not confirm a legacy protocol version"
            )

    tools_declared = "tools" in capabilities
    recorded = [
        event for event in responses if event.get("target_action_id") == "tools-list"
    ]
    if modern and not tools_declared and not recorded:
        return _Catalog(version, transport, False, {})
    if len(recorded) != 1:
        raise ReportError("expected one tools/list response")
    listed = recorded[0]
    payload = listed.get("payload")
    if listed.get("outcome") != "success":
        if (
            not modern
            and not tools_declared
            and isinstance(payload, dict)
            and payload.get("code") == -32601
        ):
            return _Catalog(version, transport, False, {})
        raise ReportError("tools/list did not return a catalog")
    if tool_list_problem(payload) is not None or (
        modern and payload.get("resultType") != "complete"
    ):
        raise ReportError("invalid tools/list result in check artifact")
    if "nextCursor" in payload:
        raise ReportError(
            "the check captured only the first page of a paginated tool catalog"
        )
    schemas: dict[str, dict[str, str]] = {}
    for tool in payload["tools"]:
        name = tool["name"]
        if name in schemas:
            raise ReportError("tools/list contains duplicate tool names")
        schemas[name] = {
            field: json.dumps(tool[field], sort_keys=True, allow_nan=False)
            for field in SCHEMA_FIELDS
            if field in tool
        }
    return _Catalog(version, transport, tools_declared, schemas)


def compare_catalogs(baseline: Artifact, current: Artifact) -> dict:
    """Compare saved input and output schemas without contacting either server."""
    try:
        before = _read_catalog(baseline)
    except ReportError as exc:
        raise ReportError(f"baseline: {exc}") from exc
    try:
        after = _read_catalog(current)
    except ReportError as exc:
        raise ReportError(f"current: {exc}") from exc
    if before.protocol_version != after.protocol_version:
        raise ReportError("checks used different negotiated protocol versions")
    if before.transport != after.transport:
        raise ReportError("checks used different transports")

    added = sorted(after.schemas.keys() - before.schemas.keys())
    removed = sorted(before.schemas.keys() - after.schemas.keys())
    changed = {
        name: [
            field
            for field in SCHEMA_FIELDS
            if before.schemas[name].get(field) != after.schemas[name].get(field)
        ]
        for name in sorted(before.schemas.keys() & after.schemas.keys())
        if before.schemas[name] != after.schemas[name]
    }
    capability_changed = before.tools_declared != after.tools_declared
    return {
        "schema_version": 1,
        "protocol_version": after.protocol_version,
        "transport": after.transport,
        "added": added,
        "removed": removed,
        "changed": changed,
        "tools_capability_changed": capability_changed,
        "status": "changed"
        if added or removed or changed or capability_changed
        else "match",
    }
