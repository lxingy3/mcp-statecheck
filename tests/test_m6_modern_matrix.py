import hashlib
import json
import tomllib
from pathlib import Path

import pytest

import mcp_statecheck.matrix as matrix
from mcp_statecheck.adapters import python_client
from mcp_statecheck.adapters.jsonl import Envelope
from scripts import run_m6_modern_acceptance as m6_acceptance

ROOT = Path(__file__).resolve().parents[1]
RUNNERS = {
    "python-v2": ("mcp", "2.1.1", "3.12.13"),
    "typescript-v2": ("@modelcontextprotocol/client", "2.0.0", "24.14.1"),
}
PROTOCOL_VERSION = "2026-07-28"
TRANSPORTS = ("stdio", "streamable-http")


def test_m6_check_requires_the_exact_checked_artifact_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        Path("acceptance.json"),
        *(
            Path("matrix") / transport / f"{runner_id}-{PROTOCOL_VERSION}.json"
            for transport in TRANSPORTS
            for runner_id in RUNNERS
        ),
    }
    for relative in expected:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(m6_acceptance, "CHECKED_ARTIFACTS", tmp_path)

    m6_acceptance._require_exact_checked_artifacts()

    (tmp_path / "stale.txt").write_text("stale\n", encoding="utf-8")
    with pytest.raises(
        m6_acceptance.AcceptanceError,
        match="exactly the acceptance summary and four traces",
    ):
        m6_acceptance._require_exact_checked_artifacts()


def test_m6_modern_profile_has_independent_locked_inputs() -> None:
    with (ROOT / "benchmarks" / "mcp-modern.toml").open("rb") as handle:
        config = tomllib.load(handle)
    runners = {runner["id"]: runner for runner in config["runners"]}

    assert config["schema_version"] == 1
    assert config["action_profile"] == "modern-stateless"
    assert config["protocol_versions"] == [PROTOCOL_VERSION]
    assert tuple(config["transports"]) == TRANSPORTS
    assert set(runners) == set(RUNNERS)
    for runner_id, (package, version, _) in RUNNERS.items():
        assert runners[runner_id]["package"] == package
        assert runners[runner_id]["version"] == version

    with (
        ROOT
        / "src"
        / "mcp_statecheck"
        / "adapters"
        / "python"
        / "modern"
        / "pyproject.toml"
    ).open("rb") as handle:
        manifest = tomllib.load(handle)
    assert manifest["project"]["dependencies"] == ["mcp==2.1.1"]
    lock_text = (
        ROOT / "src" / "mcp_statecheck" / "adapters" / "python" / "modern" / "uv.lock"
    ).read_text(encoding="utf-8")
    assert 'hash = "sha256:' in lock_text

    manifest = json.loads(
        (
            ROOT
            / "src"
            / "mcp_statecheck"
            / "adapters"
            / "typescript"
            / "v2"
            / "package.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["dependencies"]["@modelcontextprotocol/client"] == "2.0.0"


def test_m6_modern_actions_exclude_legacy_session_operations() -> None:
    assert [(action.kind.value, action.method) for action in matrix.MODERN_ACTIONS] == [
        ("connect", None),
        ("discover", None),
        ("request", "tools/list"),
        ("request", "tools/call"),
        ("close", None),
    ]
    assert all(
        action.protocol_version == PROTOCOL_VERSION
        for action in matrix.MODERN_ACTIONS
        if action.kind.value in {"discover", "request"}
    )


def test_python_adapter_accepts_only_the_modern_v2_action_profile() -> None:
    envelope = Envelope(
        command_id="modern-python",
        kind="run",
        payload={
            "action_profile": "modern-stateless",
            "actions": [action.to_dict() for action in matrix.MODERN_ACTIONS],
            "runner_id": "python-v2",
            "sdk_version": "2.1.1",
            "target": ["python", "server.py"],
            "transport": "stdio",
        },
    )

    profile, runner_id, version, transport, target, actions = python_client._command(
        envelope
    )

    assert profile == "modern-stateless"
    assert runner_id == "python-v2"
    assert version == "2.1.1"
    assert transport == "stdio"
    assert target == ("python", "server.py")
    assert actions == matrix.MODERN_ACTIONS


def test_modern_runtime_materializes_only_current_v2_inputs(tmp_path: Path) -> None:
    runners = matrix._load_modern_runners(matrix._modern_config())
    runtime = matrix._materialize_modern_runtime(tmp_path)

    assert set(runners) == set(RUNNERS)
    assert {
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == {
        "adapter/mcp_statecheck/__init__.py",
        "adapter/mcp_statecheck/adapters/__init__.py",
        "adapter/mcp_statecheck/adapters/jsonl.py",
        "adapter/mcp_statecheck/adapters/python_client.py",
        "adapter/mcp_statecheck/model.py",
        "python/modern/pyproject.toml",
        "python/modern/uv.lock",
        "typescript/v2/package-lock.json",
        "typescript/v2/package.json",
        "typescript_client.mts",
    }
    assert runtime.python_environments == {"python-v2": tmp_path / "python/modern"}
    assert runtime.typescript_environments == {
        "typescript-v2": tmp_path / "typescript/v2"
    }


def test_m6_checked_traces_are_complete_and_differentially_equal() -> None:
    directory = ROOT / "artifacts" / "m6" / "matrix"
    expected_names = {
        Path(transport) / f"{runner_id}-{PROTOCOL_VERSION}.json"
        for transport in TRANSPORTS
        for runner_id in RUNNERS
    }

    assert {
        path.relative_to(directory) for path in directory.rglob("*.json")
    } == expected_names

    for transport in TRANSPORTS:
        expected_events = matrix._modern_expected_events(transport)
        for runner_id, (_, sdk_version, runtime_version) in RUNNERS.items():
            artifact = json.loads(
                (
                    directory / transport / f"{runner_id}-{PROTOCOL_VERSION}.json"
                ).read_text(encoding="utf-8")
            )
            assert artifact["schema_version"] == 1
            assert artifact["adapter"] == runner_id
            assert artifact["sdk_version"] == sdk_version
            assert artifact["protocol_version"] == PROTOCOL_VERSION
            assert artifact["transport"] == transport
            assert artifact["fixture_id"] == "sdk-client-modern-smoke"
            assert artifact["generation"] == {
                "action_profile": "modern-stateless",
                "engine": "real SDK client transport matrix",
                "runner_id": runner_id,
                "runtime_version": runtime_version,
            }
            assert [
                (action["kind"], action["method"])
                for action in artifact["canonical_actions"]
            ] == [
                ("connect", None),
                ("discover", None),
                ("request", "tools/list"),
                ("request", "tools/call"),
                ("close", None),
            ]
            events = [
                {key: value for key, value in event.items() if key != "sequence"}
                for event in artifact["normalized_events"]
            ]
            assert events == expected_events
            if transport == "stdio":
                assert artifact["cleanup"] == {
                    "adapter_reaped": True,
                    "adapter_returncode": 0,
                    "client_closed": True,
                    "hang_probe_adapter_reaped": True,
                    "hang_probe_peer_reaped": True,
                    "peer_clean_exit": True,
                    "peer_reaped": True,
                }
            else:
                assert artifact["cleanup"] == {
                    "adapter_reaped": True,
                    "adapter_returncode": 0,
                    "client_closed": True,
                    "hang_probe_adapter_reaped": True,
                    "hang_probe_listener_closed": True,
                    "listener_closed": True,
                    "session_absent": True,
                }


def test_m6_acceptance_summary_matches_checked_trace_hashes() -> None:
    directory = ROOT / "artifacts" / "m6"
    summary = json.loads((directory / "acceptance.json").read_text(encoding="utf-8"))
    traces = {
        path.relative_to(directory / "matrix").as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted((directory / "matrix").rglob("*.json"))
    }

    assert summary["schema_version"] == 1
    assert summary["milestone"] == "M6.1"
    assert summary["slice"] == "modern-stateless-client-matrix"
    assert summary["status"] == "passed"
    assert summary["action_profile"] == "modern-stateless"
    assert summary["protocol_version"] == PROTOCOL_VERSION
    assert summary["matrix"] == {
        "byte_identical": True,
        "cells": 4,
        "golden_match": True,
        "runs": 3,
        "traces": traces,
    }
    assert all(summary["wire_invariants"].values())
    assert summary["failure_cleanup"] == {
        "adapter_reaped": True,
        "hard_timeout_seconds": 5,
        "http_listener_closed": True,
        "probes": 4,
        "stdio_peer_reaped": True,
    }
