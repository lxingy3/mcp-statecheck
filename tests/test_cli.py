from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_statecheck.cli as cli
import mcp_statecheck.replay as replay_module
from mcp_statecheck.execution import ExecutionResult
from mcp_statecheck.model import ActionKind
from mcp_statecheck.replay import ReplayInfrastructureError
from mcp_statecheck.transports import StdioProtocolError, StdioTimeout

ROOT = Path(__file__).resolve().parents[1]
PASS_ARTIFACT = ROOT / "artifacts" / "m3" / "stdio" / "python-v1-2025-06-18.json"
FAILURE_ARTIFACT = ROOT / "artifacts" / "m2" / "request-before-initialized.json"


def _events(
    *,
    initialize_outcome: str = "success",
    initialize_payload: object | None = None,
) -> tuple[dict[str, object], ...]:
    if initialize_payload is None:
        initialize_payload = {
            "capabilities": {"tools": {}},
            "protocolVersion": "2025-11-25",
            "serverInfo": {"name": "test-server", "version": "1.0"},
        }
    return (
        {
            "kind": "notification",
            "method": "notifications/message",
            "payload": {"level": "info"},
            "target_action_id": None,
        },
        {
            "kind": "response",
            "mcp_request_id": 1,
            "outcome": initialize_outcome,
            "payload": initialize_payload,
            "target_action_id": "initialize",
        },
        {
            "kind": "response",
            "mcp_request_id": 2,
            "outcome": "success",
            "payload": {},
            "target_action_id": "ping",
        },
        {
            "kind": "response",
            "mcp_request_id": 3,
            "outcome": "success",
            "payload": {"tools": []},
            "target_action_id": "tools-list",
        },
    )


def _assert_actions(actions: tuple[object, ...]) -> None:
    assert [action.action_id for action in actions] == [
        "initialize",
        "initialize-response",
        "initialized",
        "ping",
        "ping-response",
        "tools-list",
        "tools-list-response",
    ]
    assert [action.kind for action in actions] == [
        ActionKind.INITIALIZE,
        ActionKind.RESPONSE,
        ActionKind.INITIALIZED,
        ActionKind.REQUEST,
        ActionKind.RESPONSE,
        ActionKind.REQUEST,
        ActionKind.RESPONSE,
    ]
    assert [actions[index].target_action_id for index in (1, 4, 6)] == [
        "initialize",
        "ping",
        "tools-list",
    ]


def test_stdio_check_runs_exact_smoke_sequence_and_writes_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "run.json"
    junit = tmp_path / "run.xml"
    sarif = tmp_path / "run.sarif"
    html = tmp_path / "run.html"

    async def execute(
        actions: tuple[object, ...],
        command: tuple[str, ...],
        *,
        timeout: float,
    ) -> ExecutionResult:
        _assert_actions(actions)
        assert command == ("python", "server.py", "--flag")
        assert timeout == 7
        return ExecutionResult(_events(), 0, "diagnostic")

    monkeypatch.setattr(cli, "execute_stdio", execute)
    result = cli.main(
        [
            "check",
            "--stdio",
            "--timeout",
            "7",
            "--output",
            str(artifact),
            "--junit",
            str(junit),
            "--sarif",
            str(sarif),
            "--html",
            str(html),
            "--",
            "python",
            "server.py",
            "--flag",
        ]
    )

    assert result == 0
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
    assert saved["transport"] == "stdio"
    assert saved["generation"] == {
        "engine": "mcp-statecheck CLI",
        "outcome": "passed",
        "profile": "quick",
    }
    assert saved["cleanup"] == {
        "server_reaped": True,
        "server_returncode": 0,
    }
    assert [entry["sequence"] for entry in saved["canonical_actions"]] == list(
        range(1, 8)
    )
    assert [entry["sequence"] for entry in saved["normalized_events"]] == list(
        range(8, 12)
    )
    assert "failure" not in saved
    ET.parse(junit)
    assert json.loads(sarif.read_text(encoding="utf-8"))["version"] == "2.1.0"
    assert "<!doctype html>" in html.read_text(encoding="utf-8")


def test_http_check_reads_header_from_environment_without_persisting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "run.json"
    monkeypatch.setenv("MCP_TOKEN", "top-secret-value")

    async def execute(
        actions: tuple[object, ...],
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> ExecutionResult:
        _assert_actions(actions)
        assert url == "https://example.test/mcp"
        assert headers == {"Authorization": "top-secret-value"}
        assert timeout == 5
        return ExecutionResult(
            _events(),
            None,
            "",
            cleanup={"client_closed": True},
        )

    monkeypatch.setattr(cli, "execute_http", execute)
    result = cli.main(
        [
            "check",
            "--url",
            "https://example.test/mcp",
            "--header-env",
            "Authorization=MCP_TOKEN",
            "--output",
            str(artifact),
        ]
    )

    assert result == 0
    saved = artifact.read_text(encoding="utf-8")
    assert "top-secret-value" not in saved
    assert "Authorization" not in saved
    assert json.loads(saved)["cleanup"] == {"client_closed": True}


@pytest.mark.parametrize(
    "arguments",
    (
        ["check", "--stdio", "python", "server.py"],
        ["check", "--stdio", "--header-env", "Authorization=MCP_TOKEN", "--", "x"],
        ["check", "--url", "https://example.test/mcp", "extra"],
        ["check", "--url", "ftp://example.test/mcp"],
        ["check", "--url", "https://user:pass@example.test/mcp"],
        ["check", "--url", "https://example.test/mcp#fragment"],
    ),
)
def test_check_rejects_unsafe_or_ambiguous_targets(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_TOKEN", "secret")
    with pytest.raises(SystemExit) as raised:
        cli.main(arguments)
    assert raised.value.code == 2


@pytest.mark.parametrize(
    "url",
    (
        "https://example.test:abc/mcp",
        "https://example.test:99999/mcp",
        "https://:443/mcp",
    ),
)
def test_check_rejects_invalid_http_authority_before_starting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    async def execute(*_args: object, **_kwargs: object) -> ExecutionResult:
        raise AssertionError("invalid URL must not reach the HTTP transport")

    monkeypatch.setattr(cli, "execute_http", execute)
    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "check",
                "--url",
                url,
                "--output",
                str(tmp_path / "run.json"),
            ]
        )

    assert raised.value.code == 2


@pytest.mark.parametrize(
    "assignment",
    (
        "Authorization",
        "=MCP_TOKEN",
        "Bad Header=MCP_TOKEN",
        "Authorization=MISSING_TOKEN",
    ),
)
def test_header_environment_reference_is_strict(
    tmp_path: Path,
    assignment: str,
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "check",
                "--url",
                "https://example.test/mcp",
                "--header-env",
                assignment,
                "--output",
                str(tmp_path / "run.json"),
            ]
        )
    assert raised.value.code == 2


@pytest.mark.parametrize("value", ("\x01", "\n", "t\u00f6ken"))
def test_header_environment_value_must_be_ascii(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("MCP_TOKEN", value)
    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "check",
                "--url",
                "https://example.test/mcp",
                "--header-env",
                "Authorization=MCP_TOKEN",
                "--output",
                str(tmp_path / "run.json"),
            ]
        )
    assert raised.value.code == 2


def test_duplicate_http_header_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOKEN_A", "a")
    monkeypatch.setenv("TOKEN_B", "b")
    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "check",
                "--url",
                "https://example.test/mcp",
                "--header-env",
                "Authorization=TOKEN_A",
                "--header-env",
                "authorization=TOKEN_B",
                "--output",
                str(tmp_path / "run.json"),
            ]
        )
    assert raised.value.code == 2


def test_protocol_error_is_exit_one_and_writes_redacted_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "failure.json"
    monkeypatch.setenv("MCP_TOKEN", "top-secret-value")

    async def execute(*_args: object, **_kwargs: object) -> ExecutionResult:
        raise StdioProtocolError("invalid message containing top-secret-value")

    monkeypatch.setattr(cli, "execute_stdio", execute)
    result = cli.main(
        [
            "check",
            "--stdio",
            "--output",
            str(artifact),
            "--",
            "python",
            "server.py",
        ]
    )

    assert result == 1
    text = artifact.read_text(encoding="utf-8")
    assert "top-secret-value" not in text
    saved = json.loads(text)
    assert saved["failure"]["kind"] == "transport.stdio_invalid_message"
    assert saved["failure"]["signature"].startswith("mcp-statecheck:v1:")
    assert saved["failure"]["minimized_reproducer"]
    assert saved["normalized_events"] == [
        {
            "kind": "protocol_error",
            "message": "invalid message containing [REDACTED]",
            "sequence": 8,
            "target_action_id": None,
        }
    ]


def test_unpaired_surrogate_from_stdio_still_writes_failure_artifact(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "surrogate.json"
    script = (
        "import sys; sys.stdin.readline(); "
        """print('{"jsonrpc":"2.0","id":1,"result":{"text":"\\\\ud800"}}', """
        "flush=True); sys.stdin.read()"
    )

    result = cli.main(
        [
            "check",
            "--stdio",
            "--output",
            str(artifact),
            "--",
            sys.executable,
            "-c",
            script,
        ]
    )

    assert result == 1
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert saved["failure"]["kind"] == "transport.stdio_invalid_message"


def test_infrastructure_error_is_exit_two_and_not_a_protocol_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "infrastructure.json"

    async def execute(*_args: object, **_kwargs: object) -> ExecutionResult:
        raise StdioTimeout("reading from the child timed out")

    monkeypatch.setattr(cli, "execute_stdio", execute)
    result = cli.main(
        [
            "check",
            "--stdio",
            "--output",
            str(artifact),
            "--",
            "python",
            "server.py",
        ]
    )

    assert result == 2
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert "failure" not in saved
    assert saved["generation"]["outcome"] == "infrastructure_error"
    assert saved["normalized_events"][0]["kind"] == "infrastructure_error"
    html = tmp_path / "infrastructure.html"
    assert cli.main(["report", str(artifact), "--html", str(html)]) == 2
    assert "Check did not complete" in html.read_text(encoding="utf-8")


def test_check_rejects_output_alias_before_overwriting_or_starting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.write_bytes(b"keep this evidence")

    async def execute(*_args: object, **_kwargs: object) -> ExecutionResult:
        raise AssertionError("target must not start")

    monkeypatch.setattr(cli, "execute_stdio", execute)
    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "check",
                "--stdio",
                "--output",
                str(evidence),
                "--html",
                str(evidence),
                "--",
                "python",
                "server.py",
            ]
        )

    assert raised.value.code == 2
    assert evidence.read_bytes() == b"keep this evidence"


def test_invalid_initialize_result_is_a_stable_protocol_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "failure.json"

    async def execute(*_args: object, **_kwargs: object) -> ExecutionResult:
        return ExecutionResult(
            _events(initialize_outcome="error", initialize_payload={}),
            0,
            "",
        )

    monkeypatch.setattr(cli, "execute_stdio", execute)
    result = cli.main(
        [
            "check",
            "--stdio",
            "--output",
            str(artifact),
            "--",
            "python",
            "server.py",
        ]
    )

    assert result == 1
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert saved["failure"]["kind"] == "protocol.invalid_initialize_result"
    assert [
        action["action_id"] for action in saved["failure"]["minimized_reproducer"]
    ] == ["initialize", "initialize-response"]


def test_schema_valid_empty_server_info_and_duplicate_tool_names_do_not_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "run.json"
    events = list(
        _events(
            initialize_payload={
                "capabilities": {"tools": {}},
                "protocolVersion": "2025-11-25",
                "serverInfo": {"name": "", "version": ""},
            }
        )
    )
    events[-1] = {
        **events[-1],
        "payload": {
            "tools": [
                {"inputSchema": {"type": "object"}, "name": "same"},
                {"inputSchema": {"type": "object"}, "name": "same"},
            ]
        },
    }

    async def execute(*_args: object, **_kwargs: object) -> ExecutionResult:
        return ExecutionResult(tuple(events), 0, "")

    monkeypatch.setattr(cli, "execute_stdio", execute)
    result = cli.main(
        [
            "check",
            "--stdio",
            "--output",
            str(artifact),
            "--",
            "python",
            "server.py",
        ]
    )

    assert result == 0
    assert "failure" not in json.loads(artifact.read_text(encoding="utf-8"))


def test_http_status_is_an_infrastructure_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = 401

    async def execute(*_args: object, **_kwargs: object) -> ExecutionResult:
        return ExecutionResult(
            (
                {
                    "http_method": "POST",
                    "kind": "http_error",
                    "status": status,
                    "target_action_id": "initialize",
                },
            ),
            None,
            "",
            cleanup={"client_closed": True},
        )

    monkeypatch.setattr(cli, "execute_http", execute)
    first = tmp_path / "auth.json"
    assert (
        cli.main(
            [
                "check",
                "--url",
                "https://example.test/mcp",
                "--output",
                str(first),
            ]
        )
        == 2
    )
    assert "failure" not in json.loads(first.read_text(encoding="utf-8"))

    status = 503
    second = tmp_path / "server-error.json"
    assert (
        cli.main(
            [
                "check",
                "--url",
                "https://example.test/mcp",
                "--output",
                str(second),
            ]
        )
        == 2
    )
    saved = json.loads(second.read_text(encoding="utf-8"))
    assert "failure" not in saved
    assert saved["generation"]["outcome"] == "infrastructure_error"


@pytest.mark.parametrize(
    ("artifact", "expected_exit"),
    ((PASS_ARTIFACT, 0), (FAILURE_ARTIFACT, 1)),
)
def test_report_exit_code_tracks_artifact_failure(
    tmp_path: Path,
    artifact: Path,
    expected_exit: int,
) -> None:
    output = tmp_path / f"{expected_exit}.html"
    result = cli.main(["report", str(artifact), "--html", str(output)])
    assert result == expected_exit
    assert "<!doctype html>" in output.read_text(encoding="utf-8")


def test_report_requires_an_output() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["report", str(PASS_ARTIFACT)])
    assert raised.value.code == 2


def test_report_non_utf8_artifact_is_infrastructure_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "artifact.json"
    output = tmp_path / "report.html"
    source.write_bytes(b"\xff")

    assert cli.main(["report", str(source), "--html", str(output)]) == 2

    captured = capsys.readouterr()
    assert "not valid UTF-8" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""
    assert not output.exists()


def test_version_uses_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out == "mcp-statecheck 0.1.0\n"


def test_replay_returns_failure_for_a_stable_reproducer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = tmp_path / "failure.json"
    artifact.write_text("{}", encoding="utf-8")

    async def replay(*_args: object) -> object:
        return SimpleNamespace(
            expected_signature="mcp-statecheck:v1:stable",
            attempts=(object(),) * 10,
        )

    monkeypatch.setattr(replay_module, "replay_artifact", replay)

    assert cli.main(["replay", str(artifact)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "in 10/10 attempts" in captured.err


def test_replay_rejects_invalid_recipes_as_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = tmp_path / "failure.json"
    artifact.write_text("{}", encoding="utf-8")

    async def replay(*_args: object) -> object:
        raise ReplayInfrastructureError("unsupported target_recipe")

    monkeypatch.setattr(replay_module, "replay_artifact", replay)

    assert cli.main(["replay", str(artifact)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unsupported target_recipe" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("raw", ("not-json", "[1]", '{"command":"check"}'))
def test_action_boundary_rejects_invalid_arguments_as_infrastructure(
    raw: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MCP_STATECHECK_ARGUMENTS", raw)

    assert cli.action_main() == 2
    assert "arguments must be" in capsys.readouterr().err
