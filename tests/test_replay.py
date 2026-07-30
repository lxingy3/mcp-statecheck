from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest

import mcp_statecheck._controlled_peer as controlled_peer
from mcp_statecheck.execution import ExecutionResult
from mcp_statecheck.invariants import detect_failure
from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.replay import (
    ReplayInfrastructureError,
    controlled_target_recipe,
    replay_artifact,
    replay_http_failure,
    replay_stdio_failure,
)

PEER = Path(__file__).parent / "fixtures" / "peer.py"
ROOT = Path(__file__).resolve().parents[1]
STDIO_ARTIFACT = ROOT / "artifacts" / "m2" / "request-before-initialized.json"
HTTP_ARTIFACT = ROOT / "artifacts" / "m2" / "http-error-as-timeout.json"


def _with_recipe(source: Path, output: Path) -> Path:
    artifact = json.loads(source.read_text(encoding="utf-8"))
    artifact["target_recipe"] = controlled_target_recipe(artifact["fixture_id"])
    output.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def test_minimized_failure_replays_ten_times_with_one_signature() -> None:
    actions = (
        Action(
            "initialize-pending",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
            capabilities={},
        ),
        Action(
            "tools-list-2",
            ActionKind.REQUEST,
            mcp_request_id=2,
            method="tools/list",
            payload={},
        ),
    )
    failure = detect_failure(actions, ())
    assert failure is not None

    async def scenario() -> None:
        result = await replay_stdio_failure(
            actions,
            (
                sys.executable,
                str(PEER),
                "--stdio",
                "--mode",
                "request-before-initialized",
            ),
            expected_signature=failure.signature,
            attempts=10,
            timeout=5,
        )

        assert result.expected_signature == failure.signature
        assert len(result.attempts) == 10
        assert {attempt.failure.signature for attempt in result.attempts} == {
            failure.signature
        }
        assert all(attempt.execution.returncode == 0 for attempt in result.attempts)
        assert all(attempt.execution.stderr == "" for attempt in result.attempts)
        first_events = result.attempts[0].execution.events
        assert all(
            attempt.execution.events == first_events for attempt in result.attempts
        )

    anyio.run(scenario)


def test_http_failure_replays_with_ten_fresh_executions() -> None:
    actions = (
        Action("connect", ActionKind.CONNECT),
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
        ),
    )
    events = (
        {
            "fixture_source_kind": "http_error",
            "http_method": "POST",
            "kind": "timeout",
            "status": 503,
            "target_action_id": "initialize",
        },
    )
    failure = detect_failure(
        actions,
        events,
        fixture_id="http-error-as-timeout",
    )
    assert failure is not None
    calls = 0

    async def executor(
        candidate: tuple[Action, ...],
        timeout: float,
    ) -> ExecutionResult:
        nonlocal calls
        calls += 1
        assert candidate == actions
        assert timeout == 5
        return ExecutionResult(
            events,
            None,
            "",
            cleanup={"client_closed": True, "listener_closed": True},
        )

    async def scenario() -> None:
        result = await replay_http_failure(
            actions,
            executor,
            expected_signature=failure.signature,
            fixture_id="http-error-as-timeout",
            attempts=10,
            timeout=5,
        )

        assert calls == 10
        assert len(result.attempts) == 10
        assert len({id(attempt.execution) for attempt in result.attempts}) == 10
        assert all(attempt.execution.returncode is None for attempt in result.attempts)

    anyio.run(scenario)


def test_http_replay_requires_cleanup_confirmation() -> None:
    actions = (
        Action("connect", ActionKind.CONNECT),
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
        ),
    )

    async def executor(
        _: tuple[Action, ...],
        __: float,
    ) -> ExecutionResult:
        return ExecutionResult(
            (),
            None,
            "",
            cleanup={"client_closed": True, "listener_closed": False},
        )

    async def scenario() -> None:
        with pytest.raises(
            ReplayInfrastructureError,
            match=r"did not confirm listener_closed",
        ):
            await replay_http_failure(
                actions,
                executor,
                expected_signature="mcp-statecheck:v1:fixture",
                fixture_id="http-error-as-timeout",
                attempts=1,
                timeout=5,
            )

    anyio.run(scenario)


def test_http_replay_rejects_a_process_returncode() -> None:
    actions = (
        Action("connect", ActionKind.CONNECT),
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version="2025-11-25",
        ),
    )

    async def executor(
        _: tuple[Action, ...],
        __: float,
    ) -> ExecutionResult:
        return ExecutionResult((), 0, "")

    async def scenario() -> None:
        with pytest.raises(
            ReplayInfrastructureError,
            match=r"expected None",
        ):
            await replay_http_failure(
                actions,
                executor,
                expected_signature="mcp-statecheck:v1:fixture",
                fixture_id="http-error-as-timeout",
                attempts=1,
                timeout=5,
            )

    anyio.run(scenario)


def test_artifact_replay_uses_isolated_package_controlled_stdio_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _with_recipe(STDIO_ARTIFACT, tmp_path / "failure.json")
    poison = tmp_path / "mcp_statecheck"
    poison.mkdir()
    sentinel = tmp_path / "executed.txt"
    (poison / "_controlled_peer.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = anyio.run(replay_artifact, artifact, 1, 5)

    assert len(result.attempts) == 1
    assert not sentinel.exists()


def test_artifact_replay_uses_package_controlled_http_peer(tmp_path: Path) -> None:
    artifact = _with_recipe(HTTP_ARTIFACT, tmp_path / "failure.json")

    result = anyio.run(replay_artifact, artifact, 1, 5)

    assert len(result.attempts) == 1
    assert result.attempts[0].execution.cleanup == {
        "client_closed": True,
        "listener_closed": True,
    }


def test_artifact_replay_classifies_http_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _with_recipe(HTTP_ARTIFACT, tmp_path / "failure.json")

    async def fail_cleanup(*_args: object) -> ExecutionResult:
        raise RuntimeError("fixture HTTP listener still accepts connections")

    monkeypatch.setattr(
        controlled_peer,
        "execute_controlled_http_fault",
        fail_cleanup,
    )

    with pytest.raises(
        ReplayInfrastructureError,
        match="listener still accepts",
    ):
        anyio.run(replay_artifact, artifact, 1, 5)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("version", True),
        ("version", 2),
        ("kind", "command"),
        ("fixture_id", "../untrusted.py"),
    ),
)
def test_artifact_replay_rejects_untrusted_recipe_values(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    artifact = json.loads(STDIO_ARTIFACT.read_text(encoding="utf-8"))
    artifact["target_recipe"] = controlled_target_recipe(artifact["fixture_id"])
    artifact["target_recipe"][field] = value
    path = tmp_path / "failure.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(ReplayInfrastructureError):
        anyio.run(replay_artifact, path, 1, 5)


def test_artifact_replay_rejects_recipe_commands_before_execution(
    tmp_path: Path,
) -> None:
    artifact = json.loads(STDIO_ARTIFACT.read_text(encoding="utf-8"))
    sentinel = tmp_path / "executed.txt"
    artifact["target_recipe"] = {
        **controlled_target_recipe(artifact["fixture_id"]),
        "command": ["powershell", "-Command", f"Set-Content {sentinel} bad"],
    }
    path = tmp_path / "failure.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(ReplayInfrastructureError, match="fields must be exactly"):
        anyio.run(replay_artifact, path, 1, 5)
    assert not sentinel.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("fixture_id", "duplicate-concurrent-request-id"),
        ("transport", "streamable-http"),
        ("adapter", "controlled-wire"),
        ("protocol_version", "2025-06-18"),
    ),
)
def test_artifact_replay_rejects_recipe_metadata_mismatches(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    artifact = json.loads(STDIO_ARTIFACT.read_text(encoding="utf-8"))
    artifact["target_recipe"] = controlled_target_recipe("request-before-initialized")
    artifact[field] = value
    path = tmp_path / "failure.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(ReplayInfrastructureError):
        anyio.run(replay_artifact, path, 1, 5)


def test_artifact_replay_requires_a_versioned_recipe(tmp_path: Path) -> None:
    path = tmp_path / "failure.json"
    path.write_bytes(STDIO_ARTIFACT.read_bytes())
    artifact = json.loads(path.read_text(encoding="utf-8"))
    artifact.pop("target_recipe", None)
    path.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(ReplayInfrastructureError, match="target_recipe object"):
        anyio.run(replay_artifact, path, 1, 5)
