from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.run_m2_slice as m2_script
from mcp_statecheck.execution import ExecutionResult
from mcp_statecheck.fixtures import (
    FIXTURES,
    HTTP_ERROR_FIXTURE_ID,
    SSE_RESUME_FIXTURE_ID,
    fixture_by_id,
)
from mcp_statecheck.invariants import detect_failure
from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.stateful import ShrinkResult

CHECKED_IN_ARTIFACT = (
    Path(__file__).parents[1] / "artifacts" / "m2" / "request-before-initialized.json"
)
DUPLICATE_ARTIFACT = (
    Path(__file__).parents[1]
    / "artifacts"
    / "m2"
    / "duplicate-concurrent-request-id.json"
)
LATE_CORRELATION_ARTIFACT = (
    Path(__file__).parents[1]
    / "artifacts"
    / "m2"
    / "late-response-after-cancellation.json"
)
HTTP_ERROR_ARTIFACT = (
    Path(__file__).parents[1] / "artifacts" / "m2" / f"{HTTP_ERROR_FIXTURE_ID}.json"
)
SSE_RESUME_ARTIFACT = (
    Path(__file__).parents[1] / "artifacts" / "m2" / f"{SSE_RESUME_FIXTURE_ID}.json"
)


def test_m2_runner_covers_every_controlled_fixture() -> None:
    assert m2_script.M2_FIXTURE_IDS == tuple(fixture.fixture_id for fixture in FIXTURES)


def test_request_before_initialized_slice_is_deterministic(tmp_path) -> None:
    generated = tmp_path / "request-before-initialized.json"

    m2_script.build_artifact(generated, seed=20_260_716, timeout=5)

    assert generated.read_bytes() == CHECKED_IN_ARTIFACT.read_bytes()
    artifact = json.loads(generated.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    assert artifact["fixture_id"] == "request-before-initialized"
    assert artifact["protocol_version"] == "2025-11-25"
    assert artifact["generation"]["engine"] == "Hypothesis RuleBasedStateMachine"
    assert artifact["cleanup"] == {
        "shrink_peer_reaped": True,
        "shrink_peer_returncode": 0,
    }
    assert [action["sequence"] for action in artifact["canonical_actions"]] == [
        1,
        2,
    ]
    assert [event["sequence"] for event in artifact["normalized_events"]] == [
        3,
        4,
    ]
    assert artifact["failure"]["kind"] == (
        "lifecycle.client_request_before_initialize_response"
    )
    assert [
        (action["kind"], action["method"])
        for action in artifact["failure"]["minimized_reproducer"]
    ] == [("initialize", None), ("request", "tools/list")]
    assert artifact["replay"]["attempts"] == 10
    assert artifact["replay"]["matched"] == 10
    assert artifact["replay"]["returncodes"] == [0] * 10


def test_duplicate_request_id_slice_matches_its_golden_artifact(tmp_path) -> None:
    generated = tmp_path / "duplicate-concurrent-request-id.json"

    m2_script.build_artifact(
        generated,
        fixture_id="duplicate-concurrent-request-id",
        seed=20_260_717,
        timeout=5,
    )

    assert generated.read_bytes() == DUPLICATE_ARTIFACT.read_bytes()
    artifact = json.loads(generated.read_text(encoding="utf-8"))
    outbound_action_ids = [
        action["action_id"]
        for action in artifact["failure"]["minimized_reproducer"]
        if action["kind"] != "response"
    ]
    assert outbound_action_ids == list(
        fixture_by_id("duplicate-concurrent-request-id").minimum_actions
    )
    assert artifact["failure"]["kind"] == ("messages.request_id_reused_within_session")
    assert artifact["failure"]["evidence"]["overlap"] == "pending"
    assert [event["target_action_id"] for event in artifact["normalized_events"]] == [
        "initialize",
        None,
        None,
    ]
    action_sequences = {
        action["action_id"]: action["sequence"]
        for action in artifact["canonical_actions"]
    }
    initialize_event_sequence = artifact["normalized_events"][0]["sequence"]
    assert (
        action_sequences["initialize-response"]
        < initialize_event_sequence
        < action_sequences["initialized"]
    )
    assert artifact["replay"]["attempts"] == 10
    assert artifact["replay"]["matched"] == 10


def test_late_response_correlation_slice_matches_its_golden_artifact(
    tmp_path,
) -> None:
    generated = tmp_path / "late-response-after-cancellation.json"

    m2_script.build_artifact(
        generated,
        fixture_id="late-response-after-cancellation",
        seed=20_260_720,
        timeout=5,
    )

    assert generated.read_bytes() == LATE_CORRELATION_ARTIFACT.read_bytes()
    artifact = json.loads(generated.read_text(encoding="utf-8"))
    outbound_action_ids = [
        action["action_id"]
        for action in artifact["failure"]["minimized_reproducer"]
        if action["kind"] != "response"
    ]
    assert outbound_action_ids == list(
        fixture_by_id("late-response-after-cancellation").minimum_actions
    )
    assert artifact["failure"]["kind"] == (
        "differential.response_correlated_to_wrong_request"
    )
    assert artifact["failure"]["evidence"]["subject"] == "server"
    assert [event["target_action_id"] for event in artifact["normalized_events"]] == [
        "initialize",
        "call-b",
        "call-a",
    ]
    assert [
        event["payload"].get("structuredContent", {}).get("fixtureCanary")
        for event in artifact["normalized_events"][1:]
    ] == ["call-a", "call-b"]
    action_sequences = {
        action["action_id"]: action["sequence"]
        for action in artifact["canonical_actions"]
    }
    event_sequences = [event["sequence"] for event in artifact["normalized_events"]]
    assert (
        action_sequences["initialize-response"]
        < event_sequences[0]
        < action_sequences["initialized"]
    )
    assert (
        action_sequences["call-b"]
        < action_sequences["misattributed-late-response"]
        < event_sequences[1]
    )
    assert artifact["cleanup"] == {
        "shrink_peer_reaped": True,
        "shrink_peer_returncode": 0,
    }
    assert artifact["replay"]["attempts"] == 10
    assert artifact["replay"]["matched"] == 10
    assert artifact["replay"]["returncodes"] == [0] * 10


def test_http_error_timeout_slice_matches_its_golden_artifact(tmp_path) -> None:
    generated = tmp_path / HTTP_ERROR_ARTIFACT.name

    m2_script.build_artifact(
        generated,
        fixture_id=HTTP_ERROR_FIXTURE_ID,
        seed=20_260_721,
        timeout=5,
    )

    assert generated.read_bytes() == HTTP_ERROR_ARTIFACT.read_bytes()
    artifact = json.loads(generated.read_text(encoding="utf-8"))
    assert [action["action_id"] for action in artifact["canonical_actions"]] == list(
        fixture_by_id(HTTP_ERROR_FIXTURE_ID).minimum_actions
    )
    assert artifact["adapter"] == "controlled-wire"
    assert artifact["transport"] == "streamable-http"
    assert artifact["failure"]["kind"] == (
        "differential.http_status_reported_as_timeout"
    )
    assert artifact["failure"]["evidence"]["http_status"] == 503
    assert artifact["normalized_events"][0]["fixture_source_kind"] == "http_error"
    assert artifact["normalized_events"][0]["kind"] == "timeout"
    assert artifact["cleanup"] == {
        "client_closed": True,
        "listener_closed": True,
    }
    assert artifact["replay"]["matched"] == 10
    assert artifact["replay"]["returncodes"] == [None] * 10
    assert (
        artifact["replay"]["cleanups"]
        == [{"client_closed": True, "listener_closed": True}] * 10
    )


def test_second_sse_resume_slice_matches_its_golden_artifact(tmp_path) -> None:
    generated = tmp_path / SSE_RESUME_ARTIFACT.name

    m2_script.build_artifact(
        generated,
        fixture_id=SSE_RESUME_FIXTURE_ID,
        seed=20_260_722,
        timeout=5,
    )

    assert generated.read_bytes() == SSE_RESUME_ARTIFACT.read_bytes()
    artifact = json.loads(generated.read_text(encoding="utf-8"))
    assert [action["action_id"] for action in artifact["canonical_actions"]] == list(
        fixture_by_id(SSE_RESUME_FIXTURE_ID).minimum_actions
    )
    assert artifact["adapter"] == "controlled-wire"
    assert artifact["failure"]["kind"] == "differential.sse_resume_token_lost"
    assert artifact["failure"]["evidence"]["expected_last_event_id"] == "cursor-2"
    sse_events = [
        event
        for event in artifact["normalized_events"]
        if event["kind"] == "sse_resume"
    ]
    assert [event["peer_last_event_id"] for event in sse_events] == [
        None,
        "cursor-1",
        None,
    ]
    assert [event["peer_protocol_version"] for event in sse_events] == [
        "2025-11-25"
    ] * 3
    assert len({event["peer_session_id"] for event in sse_events}) == 1
    assert artifact["cleanup"] == {
        "client_closed": True,
        "listener_closed": True,
        "session_deleted": True,
    }
    assert artifact["replay"]["matched"] == 10
    assert artifact["replay"]["returncodes"] == [None] * 10
    assert (
        artifact["replay"]["cleanups"]
        == [
            {
                "client_closed": True,
                "listener_closed": True,
                "session_deleted": True,
            }
        ]
        * 10
    )


def test_replay_failure_preserves_existing_artifact(tmp_path, monkeypatch) -> None:
    output = tmp_path / "known-good.json"
    original = b'{"status":"known-good"}\n'
    output.write_bytes(original)
    actions = (
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
        ),
        Action(
            "tools-list",
            ActionKind.REQUEST,
            mcp_request_id=2,
            method="tools/list",
        ),
    )
    failure = detect_failure(actions, ())
    assert failure is not None
    generated = ShrinkResult(
        actions=actions,
        execution=ExecutionResult((), 0, ""),
        failure=failure,
        seed=20_260_716,
        hypothesis_version="test",
        settings={},
    )

    async def fail_replay(*_args) -> None:
        raise RuntimeError("controlled replay failure")

    monkeypatch.setattr(
        m2_script,
        "shrink_request_before_initialize",
        lambda *_args, **_kwargs: generated,
    )
    monkeypatch.setattr(m2_script, "_replay_saved", fail_replay)

    with pytest.raises(RuntimeError, match="controlled replay failure"):
        m2_script.build_artifact(output, seed=20_260_716, timeout=5)

    assert output.read_bytes() == original
