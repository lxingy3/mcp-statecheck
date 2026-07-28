import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mcp_statecheck.reports import (
    ReportError,
    artifact_status,
    load_artifact,
    render_html,
    render_json,
    render_junit,
    render_sarif,
    write_reports,
)

ROOT = Path(__file__).resolve().parents[1]
FAILURE_ARTIFACT = ROOT / "artifacts" / "m2" / "request-before-initialized.json"
PASS_ARTIFACT = ROOT / "artifacts" / "m3" / "stdio" / "python-v1-2025-06-18.json"


@pytest.mark.parametrize(
    ("path", "expected_failures"),
    ((FAILURE_ARTIFACT, 1), (PASS_ARTIFACT, 0)),
)
def test_checked_artifacts_render_deterministically(
    path: Path,
    expected_failures: int,
) -> None:
    artifact = load_artifact(path)
    renderers = (render_json, render_junit, render_sarif, render_html)
    for renderer in renderers:
        assert renderer(artifact) == renderer(artifact)

    assert ("failure" in json.loads(render_json(artifact))) is bool(expected_failures)
    junit = ET.fromstring(render_junit(artifact))
    assert junit.tag == "testsuite"
    assert junit.attrib["failures"] == str(expected_failures)
    assert len(junit.findall("./testcase/failure")) == expected_failures

    sarif = json.loads(render_sarif(artifact))
    assert sarif["version"] == "2.1.0"
    assert sarif["$schema"] == "https://json.schemastore.org/sarif-2.1.0.json"
    results = sarif["runs"][0]["results"]
    assert len(results) == expected_failures
    if expected_failures:
        failure = artifact["failure"]
        assert results[0]["ruleId"] == failure["kind"]
        assert results[0]["partialFingerprints"] == {
            "mcpStatecheckSignature/v1": failure["signature"]
        }
        assert "locations" not in results[0]

    page = render_html(artifact)
    assert "Content-Security-Policy" in page
    assert "Timeline" in page
    assert ("Failure detected" in page) is bool(expected_failures)


def _malicious_artifact() -> dict[str, object]:
    return {
        "adapter": "wire",
        "canonical_actions": [
            {
                "action_id": "initialize",
                "kind": "initialize",
                "sequence": 1,
            }
        ],
        "failure": {
            "evidence": {"subject": "client"},
            "kind": "bad\u0000kind",
            "minimized_reproducer": [
                {
                    "action_id": "initialize",
                    "payload": {
                        "Authorization": "Bearer explicit-secret",
                        "Cookie": "cookie-secret",
                        "message": (
                            "</pre><script>alert(1)</script> "
                            "environment-secret raw-session"
                        ),
                        "mcp_session_id": "raw-session",
                    },
                }
            ],
            "signature": "mcp-statecheck:v1:test",
            "spec_reference": "https://example.test/\u0000?<script>",
            "trigger_action_id": "initialize",
        },
        "normalized_events": [
            {
                "kind": "response",
                "payload": {
                    "Authorization": "Bearer explicit-secret",
                    "message": "environment-secret raw-session",
                    "session_id": "raw-session",
                },
                "sequence": 2,
            }
        ],
        "protocol_version": "2025-11-25",
        "schema_version": 1,
        "sdk_version": "none",
        "seed": 0,
        "transport": "stdio",
    }


def test_reports_redact_secrets_sessions_and_escape_untrusted_text() -> None:
    artifact = _malicious_artifact()
    kwargs = {
        "secret_values": ("explicit-secret",),
        "environment": {"MCP_TOKEN": "environment-secret"},
    }
    outputs = (
        render_json(artifact, **kwargs),
        render_junit(artifact, **kwargs),
        render_sarif(artifact, **kwargs),
        render_html(artifact, **kwargs),
    )
    for output in outputs:
        assert "explicit-secret" not in output
        assert "cookie-secret" not in output
        assert "environment-secret" not in output
        assert "raw-session" not in output

    json_report = json.loads(outputs[0])
    reproducer = json_report["failure"]["minimized_reproducer"][0]["payload"]
    assert reproducer["Authorization"] == "[REDACTED]"
    assert reproducer["Cookie"] == "[REDACTED]"
    assert reproducer["mcp_session_id"] == "[SESSION_1]"

    junit = ET.fromstring(outputs[1])
    assert junit.find("./testcase/failure").attrib["type"] == "bad\ufffdkind"
    sarif = json.loads(outputs[2])
    assert "helpUri" not in sarif["runs"][0]["tool"]["driver"]["rules"][0]
    assert "<script>" not in outputs[3]
    assert "&lt;script&gt;" in outputs[3]
    assert "<script" not in outputs[3].casefold()

    artifact["failure"]["spec_reference"] = "https://["
    malformed_uri_sarif = json.loads(render_sarif(artifact, **kwargs))
    assert "helpUri" not in malformed_uri_sarif["runs"][0]["tool"]["driver"]["rules"][0]


def test_infrastructure_artifact_is_not_reported_as_a_pass() -> None:
    artifact = load_artifact(PASS_ARTIFACT)
    artifact["generation"] = {
        "engine": "mcp-statecheck CLI",
        "outcome": "infrastructure_error",
        "profile": "quick",
    }
    artifact["normalized_events"].append(
        {
            "kind": "infrastructure_error",
            "message": "the server operation timed out",
            "sequence": max(
                entry["sequence"]
                for entry in (
                    artifact["canonical_actions"] + artifact["normalized_events"]
                )
            )
            + 1,
            "target_action_id": None,
        }
    )

    assert artifact_status(artifact) == "infrastructure_error"
    junit = ET.fromstring(render_junit(artifact))
    assert junit.attrib["failures"] == "0"
    assert junit.attrib["errors"] == "1"
    assert junit.find("./testcase/error").attrib["type"] == "infrastructure_error"
    sarif = json.loads(render_sarif(artifact))
    assert sarif["runs"][0]["invocations"] == [{"executionSuccessful": False}]
    page = render_html(artifact)
    assert "Check did not complete" in page
    assert "Infrastructure error" in page


def test_secret_values_cannot_change_artifact_status() -> None:
    failure = load_artifact(FAILURE_ARTIFACT)
    failure["normalized_events"].append(
        {
            "kind": "response",
            "sequence": max(
                entry["sequence"]
                for entry in (
                    failure["canonical_actions"] + failure["normalized_events"]
                )
            )
            + 1,
            "session_id": "failure",
        }
    )
    infrastructure = load_artifact(PASS_ARTIFACT)
    infrastructure["generation"] = {
        "engine": "mcp-statecheck CLI",
        "outcome": "infrastructure_error",
        "profile": "quick",
    }

    assert artifact_status(failure, environment={"TOKEN": "failure"}) == "failure"
    assert (
        artifact_status(infrastructure, environment={"TOKEN": "error"})
        == "infrastructure_error"
    )


@pytest.mark.parametrize(
    "payload",
    (
        "[]",
        '{"schema_version":2}',
        '{"schema_version":1,"schema_version":1}',
        '{"schema_version":NaN}',
    ),
)
def test_load_artifact_rejects_invalid_or_unsupported_schema(
    tmp_path: Path,
    payload: str,
) -> None:
    source = tmp_path / "artifact.json"
    source.write_text(payload, encoding="utf-8")
    with pytest.raises(ReportError):
        load_artifact(source)


def test_load_artifact_wraps_non_utf8_input(tmp_path: Path) -> None:
    source = tmp_path / "artifact.json"
    source.write_bytes(b"\xff")

    with pytest.raises(ReportError, match="not valid UTF-8") as raised:
        load_artifact(source)

    assert isinstance(raised.value.__cause__, UnicodeDecodeError)


def test_reports_reject_unpaired_unicode_surrogates() -> None:
    artifact = load_artifact(PASS_ARTIFACT)
    artifact["normalized_events"][0]["payload"] = "\ud800"

    with pytest.raises(ReportError, match="unsupported values"):
        render_html(artifact)


def test_failure_null_is_not_a_passing_artifact() -> None:
    artifact = _malicious_artifact()
    artifact["failure"] = None
    with pytest.raises(ReportError, match="failure must be an object"):
        render_json(artifact)


def test_write_reports_is_atomic_and_rejects_path_conflicts(
    tmp_path: Path,
) -> None:
    artifact = load_artifact(PASS_ARTIFACT)
    source = tmp_path / "source.json"
    source_bytes = render_json(artifact).encode()
    source.write_bytes(source_bytes)
    outputs = {
        "json_path": tmp_path / "report.json",
        "junit_path": tmp_path / "report.xml",
        "sarif_path": tmp_path / "report.sarif",
        "html_path": tmp_path / "report.html",
    }

    written = write_reports(artifact, source_path=source, **outputs)

    assert written == tuple(outputs.values())
    assert json.loads(outputs["json_path"].read_text())["schema_version"] == 1
    ET.parse(outputs["junit_path"])
    assert json.loads(outputs["sarif_path"].read_text())["version"] == "2.1.0"
    assert "<!doctype html>" in outputs["html_path"].read_text()
    assert not list(tmp_path.glob(".*.tmp"))

    with pytest.raises(ReportError, match="overwrite"):
        write_reports(artifact, source_path=source, html_path=source)
    assert source.read_bytes() == source_bytes

    duplicate = tmp_path / "same-output"
    with pytest.raises(ReportError, match="distinct"):
        write_reports(artifact, json_path=duplicate, html_path=duplicate)
    assert not duplicate.exists()
