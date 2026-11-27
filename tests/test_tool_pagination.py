from __future__ import annotations

import json
import sys

import pytest

from mcp_statecheck._controlled_peer import (
    TOOL_PAGE_CURSOR,
    ControlledHTTPPeer,
    PeerState,
)
from mcp_statecheck.cli import main

MODERN = "2026-07-28"


@pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25", MODERN])
@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_check_follows_every_page_on_one_connection(
    tmp_path, capsys, version, transport
):
    mode = "sdk-modern-paged" if version == MODERN else "sdk-paged"
    output = tmp_path / "check.json"
    args = ["check", "--protocol-version", version, "--output", str(output)]
    if transport == "stdio":
        assert (
            main(
                [
                    *args,
                    "--stdio",
                    "--",
                    sys.executable,
                    "-m",
                    "mcp_statecheck._controlled_peer",
                    "--stdio",
                    "--mode",
                    mode,
                    "--protocol-version",
                    version,
                ]
            )
            == 0
        )
    else:
        with ControlledHTTPPeer(mode, protocol_version=version) as peer:
            assert main([*args, "--url", peer.url]) == 0
            state = peer.state
            if version == MODERN:
                assert state.post_methods == ["server/discover", *["tools/list"] * 3]
                assert state.post_method_headers == state.post_methods
                assert state.post_protocol_versions == [version] * 4
                assert state.post_session_ids == [None] * 4
                assert state.delete_count == 0
            else:
                assert state.post_methods == [
                    "initialize",
                    "notifications/initialized",
                    "ping",
                    *["tools/list"] * 3,
                ]
                assert state.post_protocol_versions == [None, *[version] * 5]
                assert state.post_session_ids == [None, *[state.session_id] * 5]
                assert state.delete_count == 1

    raw = output.read_text(encoding="utf-8")
    assert TOOL_PAGE_CURSOR not in raw
    artifact = json.loads(raw)
    requests = [
        action
        for action in artifact["canonical_actions"]
        if action["method"] == "tools/list"
    ]
    assert len({action["mcp_request_id"] for action in requests}) == 3
    responses = [
        event
        for event in artifact["normalized_events"]
        if event["target_action_id"] in {action["action_id"] for action in requests}
    ]
    assert [event["payload"]["tools"][0]["name"] for event in responses] == [
        "echo",
        "status",
        "version",
    ]
    for previous, request in zip(responses[:-1], requests[1:], strict=True):
        assert request["payload"] == {"cursor": previous["payload"]["nextCursor"]}

    # Change a definition that the first page cannot reveal.
    responses[-1]["payload"]["tools"][0]["inputSchema"]["required"] = ["format"]
    current = tmp_path / "current.json"
    current.write_text(json.dumps(artifact), encoding="utf-8")
    capsys.readouterr()
    assert main(["compare", str(output), str(current)]) == 1
    comparison = json.loads(capsys.readouterr().out)
    assert comparison["added"] == comparison["removed"] == []
    assert comparison["changed"] == {"version": ["inputSchema"]}


@pytest.mark.parametrize(
    ("mode", "exit_code", "reason"),
    [
        ("budget", 2, "exceeded --max-tool-pages (2)"),
        ("cycle", 1, "repeated_next_cursor"),
        ("duplicate", 1, "duplicate_tool_name"),
        ("malformed", 1, "invalid_next_cursor"),
        ("error", 1, "page_error"),
    ],
)
def test_check_does_not_report_a_partial_catalog_as_passing(
    tmp_path, monkeypatch, mode, exit_code, reason
):
    respond = PeerState.handle_stdio

    def handle(self, message):
        replies = respond(self, message)
        if message["method"] != "tools/list" or message["params"].get("cursor") != "":
            return replies
        if mode == "cycle":
            replies[0]["result"]["nextCursor"] = ""
        elif mode == "duplicate":
            replies[0]["result"]["tools"][0]["name"] = "echo"
        elif mode == "malformed":
            replies[0]["result"]["nextCursor"] = 2
        elif mode == "error":
            replies[0].pop("result")
            replies[0]["error"] = {
                "code": -32602,
                "message": "page no longer available",
            }
        return replies

    monkeypatch.setattr(PeerState, "handle_stdio", handle)
    output = tmp_path / "check.json"
    with ControlledHTTPPeer("sdk-modern-paged", protocol_version=MODERN) as peer:
        args = [
            "check",
            "--url",
            peer.url,
            "--protocol-version",
            MODERN,
            "--output",
            str(output),
        ]
        if mode == "budget":
            args.extend(["--max-tool-pages", "2"])
        assert main(args) == exit_code
        assert peer.state.post_methods == [
            "server/discover",
            "tools/list",
            "tools/list",
        ]

    artifact = json.loads(output.read_text(encoding="utf-8"))
    if exit_code == 2:
        assert artifact["generation"]["outcome"] == "infrastructure_error"
        assert reason in artifact["normalized_events"][-1]["message"]
    else:
        assert artifact["failure"]["evidence"]["reason"] == reason
        assert artifact["failure"]["trigger_action_id"] == "tools-list-page-2"
    assert main(["compare", str(output), str(output)]) == 2


def test_compare_checks_that_continuation_request_matches_previous_page(
    tmp_path, capsys
):
    output = tmp_path / "check.json"
    with ControlledHTTPPeer("sdk-modern-paged", protocol_version=MODERN) as peer:
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
            == 0
        )
    artifact = json.loads(output.read_text(encoding="utf-8"))
    current = tmp_path / "broken.json"
    for action in artifact["canonical_actions"]:
        if action["action_id"] == "tools-list-page-2":
            action["payload"]["cursor"] = "unrelated"
    current.write_text(json.dumps(artifact), encoding="utf-8")
    capsys.readouterr()
    assert main(["compare", str(output), str(current)]) == 2
    result = capsys.readouterr()
    assert "matching continuation request" in result.err
    assert result.out == ""
