"""Compare the tool catalogs recorded by successful quick checks."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .model import MODERN_PROTOCOL_VERSIONS, PROTOCOL_VERSIONS
from .reports import Artifact, ReportError

SCHEMA_FIELDS = ("inputSchema", "outputSchema")
DEFAULT_TOOL_PAGE_LIMIT = 100


def tool_page_id(page: int) -> str:
    return "tools-list" if page == 1 else f"tools-list-page-{page}"


def is_tool_page(target: object) -> bool:
    return isinstance(target, str) and (
        target == "tools-list" or target.startswith("tools-list-page-")
    )


def tool_page_responses(
    events: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    return [
        event
        for event in events
        if event.get("kind") == "response"
        and is_tool_page(event.get("target_action_id"))
    ]


@dataclass
class ToolPages:
    modern: bool
    allow_single_page_duplicates: bool = False
    tools: dict[str, Mapping[str, object]] = field(default_factory=dict, init=False)
    next_cursor: str | None = field(default=None, init=False)
    count: int = field(default=0, init=False)
    _cursors: set[str] = field(default_factory=set, init=False)

    def add(self, payload: object) -> str | None:
        if not isinstance(payload, Mapping):
            return "invalid_completion" if self.modern else "invalid_tools_array"
        if self.modern and payload.get("resultType") != "complete":
            return "invalid_completion"
        tools = payload.get("tools")
        if not isinstance(tools, list):
            return "invalid_tools_array"
        cursor = payload.get("nextCursor")
        if cursor is not None:
            if not isinstance(cursor, str):
                return "invalid_next_cursor"
            if cursor in self._cursors:
                return "repeated_next_cursor"
        allow_duplicates = (
            self.allow_single_page_duplicates and self.count == 0 and cursor is None
        )
        page = {}
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
            name = tool["name"]
            if not allow_duplicates and (name in page or name in self.tools):
                return "duplicate_tool_name"
            page[name] = tool
        if cursor is not None:
            self._cursors.add(cursor)
        self.tools.update(page)
        self.next_cursor = cursor
        self.count += 1
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
    recorded = tool_page_responses(responses)
    if modern and not tools_declared and not recorded:
        return _Catalog(version, transport, False, {})
    if not recorded:
        raise ReportError("expected a tools/list response")
    listed = recorded[0]
    payload = listed.get("payload")
    if listed.get("outcome") != "success":
        if (
            not modern
            and not tools_declared
            and len(recorded) == 1
            and isinstance(payload, dict)
            and payload.get("code") == -32601
        ):
            return _Catalog(version, transport, False, {})
        raise ReportError("tools/list did not return a catalog")
    pages = ToolPages(modern)
    requests_by_id = {}
    for action in artifact["canonical_actions"]:
        action_id = action.get("action_id")
        if is_tool_page(action_id):
            requests_by_id.setdefault(action_id, []).append(action)
    for number, event in enumerate(recorded, start=1):
        action_id = tool_page_id(number)
        if (
            event.get("target_action_id") != action_id
            or event.get("outcome") != "success"
        ):
            raise ReportError(f"expected successful tools/list page {number}")
        if number > 1:
            requests = requests_by_id.get(action_id, ())
            if (
                pages.next_cursor is None
                or len(requests) != 1
                or requests[0].get("kind") != "request"
                or requests[0].get("method") != "tools/list"
                or requests[0].get("payload") != {"cursor": pages.next_cursor}
                or type(event.get("mcp_request_id")) is not int
                or type(requests[0].get("mcp_request_id")) is not int
                or requests[0].get("mcp_request_id") != event["mcp_request_id"]
            ):
                raise ReportError(
                    f"tools/list page {number} has no matching continuation request"
                )
        reason = pages.add(event.get("payload"))
        if reason == "duplicate_tool_name":
            raise ReportError("tools/list contains duplicate tool names")
        if reason is not None:
            raise ReportError(f"invalid tools/list page {number}: {reason}")
    if pages.next_cursor is not None:
        raise ReportError("incomplete tool catalog; run check to capture every page")
    schemas = {
        name: {
            field: json.dumps(tool[field], sort_keys=True, allow_nan=False)
            for field in SCHEMA_FIELDS
            if field in tool
        }
        for name, tool in pages.tools.items()
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
