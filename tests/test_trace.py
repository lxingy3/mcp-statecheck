import json

import pytest

from mcp_statecheck.trace import REDACTED, TraceRecorder, redact


def test_recorder_redacts_before_storage_and_aliases_sessions(tmp_path) -> None:
    explicit_secret = "explicit-secret-value"
    environment_secret = "environment-secret-value"
    session_id = "raw-session-id"
    recorder = TraceRecorder(
        protocol_version="2025-06-18",
        adapter="wire",
        sdk_version="none",
        transport="http",
        seed=17,
        secret_values=(explicit_secret,),
        environment={"MCP_TOKEN": environment_secret, "PUBLIC_NAME": "visible"},
        fixture_id="http-error",
        cleanup={"listener_closed": True},
    )

    assert (
        recorder.record_action(
            {
                "kind": "initialize",
                "headers": {
                    "Authorization": f"Bearer {explicit_secret}",
                    "Cookie": environment_secret,
                    "X-API-Key": explicit_secret,
                    "Mcp-Session-Id": session_id,
                },
                "nested": [{"password": explicit_secret}],
                "note": f"{explicit_secret}:{environment_secret}",
            }
        )
        == 1
    )
    assert (
        recorder.record_event(
            {
                "kind": "response",
                "session_id": session_id,
                "echo": session_id,
            }
        )
        == 2
    )

    artifact = recorder.artifact()
    memory_text = json.dumps(artifact, sort_keys=True)
    assert explicit_secret not in memory_text
    assert environment_secret not in memory_text
    assert session_id not in memory_text
    assert memory_text.count("[SESSION_1]") == 3
    assert artifact["canonical_actions"][0]["sequence"] == 1
    assert artifact["normalized_events"][0]["sequence"] == 2
    assert artifact["canonical_actions"][0]["headers"]["Authorization"] == REDACTED

    output = tmp_path / "failure.json"
    recorder.write(output)
    first_write = output.read_text(encoding="utf-8")
    recorder.write(output)
    assert output.read_text(encoding="utf-8") == first_write
    assert explicit_secret not in first_write
    assert environment_secret not in first_write
    assert session_id not in first_write
    assert list(tmp_path.iterdir()) == [output]


def test_artifact_is_detached_and_failure_is_single_assignment() -> None:
    recorder = TraceRecorder(
        protocol_version="2025-11-25",
        adapter="python",
        sdk_version="2.0.0",
        transport="stdio",
        seed=3,
    )
    recorder.record_action({"kind": "ping"})
    snapshot = recorder.artifact()
    snapshot["canonical_actions"].clear()

    recorder.set_failure(
        kind="protocol",
        spec_reference="lifecycle.initialize",
        signature="protocol:early-request",
        minimized_reproducer=({"kind": "ping"},),
        trigger_action_id="ping-1",
        evidence={"subject": "client"},
    )

    artifact = recorder.artifact()
    assert artifact["schema_version"] == 1
    assert len(artifact["canonical_actions"]) == 1
    assert artifact["failure"]["minimized_reproducer"] == [{"kind": "ping"}]
    assert artifact["failure"]["trigger_action_id"] == "ping-1"
    assert artifact["failure"]["evidence"] == {"subject": "client"}
    with pytest.raises(RuntimeError, match="already set"):
        recorder.set_failure(
            kind="other",
            spec_reference="other",
            signature="other",
            minimized_reproducer=(),
        )


def test_artifact_records_generation_and_replay_proof() -> None:
    recorder = TraceRecorder(
        protocol_version="2025-11-25",
        adapter="wire",
        sdk_version="none",
        transport="stdio",
        seed=17,
        generation={"engine": "hypothesis", "version": "6.0"},
    )

    recorder.set_replay(
        attempts=10,
        matched=10,
        signature="mcp-statecheck:v1:stable",
        returncodes=(0,) * 10,
        cleanups=({"child_reaped": True},) * 10,
    )

    artifact = recorder.artifact()
    assert artifact["generation"] == {
        "engine": "hypothesis",
        "version": "6.0",
    }
    assert artifact["replay"] == {
        "attempts": 10,
        "cleanups": [{"child_reaped": True}] * 10,
        "matched": 10,
        "returncodes": [0] * 10,
        "signature": "mcp-statecheck:v1:stable",
    }


def test_standalone_redaction_is_recursive_and_rejects_non_json_values() -> None:
    value = redact(
        {
            "Set-Cookie": "cookie",
            "items": ["prefix-secret", {"sessionId": "s1"}],
            "progressToken": "visible-progress",
            "resume_token": "visible-resume",
            "session": {"state": "ready", "owner": "visible-owner"},
        },
        secret_values=("secret",),
    )

    assert value == {
        "Set-Cookie": REDACTED,
        "items": ["prefix-[REDACTED]", {"sessionId": "[SESSION_1]"}],
        "progressToken": "visible-progress",
        "resume_token": "visible-resume",
        "session": {"owner": "visible-owner", "state": "ready"},
    }
    with pytest.raises(TypeError, match="unsupported trace value"):
        redact({1, 2})


def test_common_secret_field_names_are_redacted_by_default() -> None:
    value = redact(
        {
            "OPENAI_API_KEY": "openai-secret",
            "GITHUB_TOKEN": "github-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "oauthClientSecret": "oauth-secret",
            "progressToken": "progress-visible",
            "resume_token": "resume-visible",
        }
    )

    assert value == {
        "AWS_SECRET_ACCESS_KEY": REDACTED,
        "GITHUB_TOKEN": REDACTED,
        "OPENAI_API_KEY": REDACTED,
        "oauthClientSecret": REDACTED,
        "progressToken": "progress-visible",
        "resume_token": "resume-visible",
    }


def test_secret_values_do_not_corrupt_structural_keys_or_enums() -> None:
    value = redact(
        {
            "error": {"message": "error"},
            "failure": {"kind": "protocol.failure"},
            "generation": {"outcome": "infrastructure_error"},
            "note": "prefix-error error xerrory",
        },
        secret_values=("error", "failure"),
    )

    assert value["error"]["message"] == REDACTED
    assert value["failure"]["kind"] == f"protocol.{REDACTED}"
    assert value["generation"]["outcome"] == "infrastructure_error"
    assert value["note"] == f"prefix-{REDACTED} {REDACTED} x{REDACTED}y"


def test_known_session_ids_are_removed_from_embedded_and_earlier_text() -> None:
    session_id = "session-secret-value"
    recorder = TraceRecorder(
        protocol_version="2025-11-25",
        adapter="wire",
        sdk_version="none",
        transport="streamable-http",
        seed=0,
    )
    recorder.record_action({"kind": "connect", "diagnostic": f"url?sid={session_id}"})
    recorder.record_event(
        {
            "kind": "response",
            "MCP-Session-Id": session_id,
            "diagnostic": f"received:{session_id}",
        }
    )

    serialized = json.dumps(recorder.artifact(), sort_keys=True)
    assert session_id not in serialized
    assert serialized.count("[SESSION_1]") == 3


def test_short_and_overlapping_session_ids_do_not_corrupt_artifact_shape() -> None:
    recorder = TraceRecorder(
        protocol_version="2025-11-25",
        adapter="adapter",
        sdk_version="none",
        transport="streamable-http",
        seed=0,
    )
    recorder.record_event(
        {
            "kind": "sessions",
            "sessionId": "a",
            "nested": {"session_id": "aa"},
            "diagnostic": "sid=a&next=aa",
        }
    )

    artifact = recorder.artifact()
    assert artifact["adapter"] == "adapter"
    assert "canonical_actions" in artifact
    assert "normalized_events" in artifact
    event = artifact["normalized_events"][0]
    assert event["sessionId"] == "[SESSION_1]"
    assert event["nested"]["session_id"] == "[SESSION_2]"
    assert event["diagnostic"] == "sid=[SESSION_1]&next=[SESSION_2]"
