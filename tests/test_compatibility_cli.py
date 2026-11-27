from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mcp_statecheck._controlled_peer import ControlledHTTPPeer, PeerState
from mcp_statecheck.cli import main

MODERN = "2026-07-28"


@pytest.mark.parametrize(
    ("variant", "methods", "status", "reason"),
    [
        ("tools", ["server/discover", "tools/list"], 0, None),
        ("no-tools", ["server/discover"], 0, None),
        ("other-version", ["server/discover"], 1, "unsupported_protocol_version"),
        (
            "invalid-list",
            ["server/discover", "tools/list"],
            1,
            "invalid_completion",
        ),
    ],
)
def test_modern_check_uses_discovery_before_listing(
    tmp_path, monkeypatch, variant, methods, status, reason
):
    respond = PeerState.handle_stdio

    def discovery_variant(self, message):
        replies = respond(self, message)
        if message["method"] == "server/discover":
            result = replies[0]["result"]
            if variant == "no-tools":
                result["capabilities"] = {"resources": {}}
            elif variant == "other-version":
                result["supportedVersions"] = ["2025-11-25"]
        if message["method"] == "tools/list" and variant == "invalid-list":
            replies[0]["result"].pop("resultType")
        return replies

    monkeypatch.setattr(PeerState, "handle_stdio", discovery_variant)
    output = tmp_path / "check.json"
    with ControlledHTTPPeer("sdk-modern-smoke", protocol_version=MODERN) as peer:
        assert (
            main(
                [
                    "check",
                    "--url",
                    peer.url,
                    "--protocol-version",
                    MODERN,
                    "--output",
                    str(output),
                ]
            )
            == status
        )
        assert peer.state.post_methods == methods
        assert peer.state.post_method_headers == methods
        assert peer.state.post_protocol_versions == [MODERN] * len(methods)
        assert peer.state.post_session_ids == [None] * len(methods)
        assert peer.state.request_meta_valid == [True] * len(methods)
        assert peer.state.get_count == peer.state.delete_count == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["protocol_version"] == MODERN
    action_ids = [action["action_id"] for action in saved["canonical_actions"]]
    assert ("tools-list" in action_ids) == ("tools/list" in methods)
    if reason:
        assert saved["failure"]["evidence"]["reason"] == reason
    else:
        assert saved["generation"]["outcome"] == "passed"


def test_modern_stdio_check_and_offline_compare_use_the_same_artifact(tmp_path, capsys):
    output = tmp_path / "check.json"
    assert (
        main(
            [
                "check",
                "--stdio",
                "--protocol-version",
                MODERN,
                "--output",
                str(output),
                "--",
                sys.executable,
                "-m",
                "mcp_statecheck._controlled_peer",
                "--stdio",
                "--mode",
                "sdk-modern-smoke",
                "--protocol-version",
                MODERN,
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["compare", str(output), str(output)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "protocol_version": MODERN,
        "transport": "stdio",
        "added": [],
        "removed": [],
        "changed": {},
        "tools_capability_changed": False,
        "status": "match",
    }


def _legacy_trace(negotiated, tools, **extras):
    def response(sequence, action_id, payload):
        return {
            "sequence": sequence,
            "kind": "response",
            "target_action_id": action_id,
            "outcome": "success",
            "payload": payload,
        }

    return {
        "schema_version": 1,
        "protocol_version": "2025-11-25",
        "transport": "stdio",
        "adapter": "wire",
        "sdk_version": "none",
        "seed": 0,
        "fixture_id": "server-smoke",
        "generation": {"profile": "quick", "outcome": "passed"},
        "canonical_actions": [],
        "normalized_events": [
            response(
                1,
                "initialize",
                {"protocolVersion": negotiated, "capabilities": {"tools": {}}},
            ),
            response(2, "tools-list", {"tools": tools, **extras}),
        ],
    }


def _compare(tmp_path: Path, baseline: dict, current: dict) -> int:
    paths = (tmp_path / "baseline.json", tmp_path / "current.json")
    for path, value in zip(paths, (baseline, current), strict=True):
        path.write_text(json.dumps(value), encoding="utf-8")
    return main(["compare", *(str(path) for path in paths)])


def test_compare_names_schema_fields_and_ignores_catalog_order_and_descriptions(
    tmp_path, capsys
):
    baseline = _legacy_trace(
        "2025-06-18",
        [
            {
                "name": "keep",
                "description": "old",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "schema",
                "inputSchema": {"properties": {"flag": {"const": True}}},
            },
            {"name": "remove", "inputSchema": {}},
        ],
    )
    current = _legacy_trace(
        "2025-06-18",
        [
            {"name": "add", "inputSchema": {}},
            {
                "name": "schema",
                "inputSchema": {"properties": {"flag": {"const": 1}}},
                "outputSchema": {},
            },
            {
                "name": "keep",
                "description": "new",
                "inputSchema": {"properties": {}, "type": "object"},
            },
        ],
    )
    assert _compare(tmp_path, baseline, current) == 1
    diff = json.loads(capsys.readouterr().out)
    assert diff["protocol_version"] == "2025-06-18"
    assert diff["added"] == ["add"]
    assert diff["removed"] == ["remove"]
    assert diff["changed"] == {"schema": ["inputSchema", "outputSchema"]}


@pytest.mark.parametrize(
    ("current", "message"),
    [
        (_legacy_trace("2025-11-25", [], nextCursor="next"), "incomplete tool catalog"),
        (_legacy_trace("2025-06-18", []), "different negotiated protocol versions"),
        (
            _legacy_trace("2025-11-25", [{"name": "echo", "inputSchema": {}}] * 2),
            "duplicate tool names",
        ),
        (
            _legacy_trace("2025-11-25", [])
            | {"generation": {"outcome": "infrastructure_error"}},
            "successful check",
        ),
    ],
)
def test_compare_rejects_incomplete_or_incompatible_evidence(
    tmp_path, capsys, current, message
):
    assert _compare(tmp_path, _legacy_trace("2025-11-25", []), current) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
