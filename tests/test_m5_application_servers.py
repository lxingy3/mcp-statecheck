import hashlib
import json
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import run_m5_filesystem_acceptance, run_m5_git_acceptance

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "m5" / "filesystem"
FILESYSTEM_PACKAGE = "@modelcontextprotocol/server-filesystem"
FILESYSTEM_VERSION = "2026.7.10"
FILESYSTEM_INTEGRITY = "sha512-Mmjg4anFBD5OzbPnGJOA0jPPN8645ERhQk38HQLpSenx1ox9bfdPkmAzUnNjeQtqQGFLtKe13J20RtLBmUKMZA=="
FILESYSTEM_RESOLVED = (
    "https://registry.npmjs.org/@modelcontextprotocol/server-filesystem/-/"
    "server-filesystem-2026.7.10.tgz"
)
GIT_PACKAGE = "mcp-server-git"
GIT_VERSION = "2026.8.18"
GIT_WHEEL_SHA256 = "6c32a8e771564122a9bafac373cf871fb3ab540ddc1ba0ee8e9c8c6e9878aef7"
GIT_SDIST_SHA256 = "96894ca661cfda45174a8882d4ca97d1f5261301c72ccad006ff04f4108f173f"


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_m52_filesystem_server_mutates_only_its_sandbox() -> None:
    benchmark = ROOT / "benchmarks" / "external" / "server-filesystem"
    manifest = _load(benchmark / "package.json")
    lock_path = benchmark / "package-lock.json"
    lock = _load(lock_path)
    locked_package = lock["packages"][f"node_modules/{FILESYSTEM_PACKAGE}"]

    assert manifest["private"] is True
    assert manifest["engines"] == {"node": "24.14.1"}
    assert manifest["dependencies"] == {FILESYSTEM_PACKAGE: FILESYSTEM_VERSION}
    assert lock["lockfileVersion"] == 3
    assert locked_package["version"] == FILESYSTEM_VERSION
    assert locked_package["resolved"] == FILESYSTEM_RESOLVED
    assert locked_package["integrity"] == FILESYSTEM_INTEGRITY
    assert locked_package["bin"] == {
        "mcp-server-filesystem": "dist/index.js",
    }

    trace_path = ARTIFACTS / f"filesystem-{FILESYSTEM_VERSION}-stdio.json"
    trace = _load(trace_path)
    assert trace["fixture_id"] == "filesystem-sandbox-state"
    assert trace["protocol_version"] == "2025-11-25"
    assert trace["transport"] == "stdio"
    assert trace["generation"] == {
        "engine": "mcp-statecheck M5 acceptance",
        "outcome": "passed",
        "profile": "application-state",
    }
    assert trace["cleanup"] == {
        "server_reaped": True,
        "server_returncode": 0,
    }
    assert "failure" not in trace
    serialized_trace = trace_path.read_text(encoding="utf-8")
    assert "<sandbox>" in serialized_trace
    assert str(ROOT) not in serialized_trace

    responses = {
        event["target_action_id"]: event
        for event in trace["normalized_events"]
        if event["kind"] == "response"
    }
    for action_id in (
        "initialize",
        "tools-list",
        "write-file",
        "read-file",
        "write-outside",
    ):
        assert responses[action_id]["outcome"] == "success"
    assert responses["write-outside"]["payload"]["isError"] is True

    acceptance = _load(ARTIFACTS / "acceptance.json")
    filesystem = acceptance["targets"]["filesystem"]
    assert acceptance["slice"] == "filesystem-application-server"
    assert filesystem["package"] == {
        "ecosystem": "npm",
        "integrity": locked_package["integrity"],
        "name": FILESYSTEM_PACKAGE,
        "version": FILESYSTEM_VERSION,
    }
    assert filesystem["runs"] == {
        "attempted": 10,
        "byte_identical": True,
        "passed": 10,
    }
    assert filesystem["state"] == {
        "outside_sentinel_unchanged": 10,
        "outside_write_rejected": 10,
        "written_content_verified": 10,
    }
    assert filesystem["trace"] == {
        "file": trace_path.name,
        "sha256": _sha256(trace_path),
        "sha256_counts": {_sha256(trace_path): 10},
    }
    assert filesystem["lock_sha256"] == _sha256(lock_path)


def _tool_rejected(response: dict[str, object]) -> bool:
    if response["outcome"] == "error":
        return True
    payload = response["payload"]
    return isinstance(payload, dict) and payload.get("isError") is True


def test_m52_git_server_mutates_only_its_allowed_repository() -> None:
    benchmark = ROOT / "benchmarks" / "external" / "server-git"
    with (benchmark / "pyproject.toml").open("rb") as handle:
        manifest = tomllib.load(handle)
    with (benchmark / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)

    assert manifest["project"] == {
        "name": "mcp-statecheck-server-git-benchmark",
        "version": "0.0.0",
        "requires-python": ">=3.12,<3.13",
        "dependencies": [f"{GIT_PACKAGE}=={GIT_VERSION}"],
    }
    locked_package = next(
        package for package in lock["package"] if package["name"] == GIT_PACKAGE
    )
    assert locked_package["version"] == GIT_VERSION
    assert locked_package["source"] == {"registry": "https://pypi.org/simple"}
    assert locked_package["sdist"]["hash"] == f"sha256:{GIT_SDIST_SHA256}"
    assert {wheel["hash"] for wheel in locked_package["wheels"]} == {
        f"sha256:{GIT_WHEEL_SHA256}"
    }

    artifacts = ROOT / "artifacts" / "m5" / "git"
    trace_path = artifacts / f"git-{GIT_VERSION}-stdio.json"
    trace = _load(trace_path)
    assert trace["fixture_id"] == "git-repository-state"
    assert trace["protocol_version"] == "2025-11-25"
    assert trace["transport"] == "stdio"
    assert trace["generation"] == {
        "engine": "mcp-statecheck M5 acceptance",
        "outcome": "passed",
        "profile": "application-state",
    }
    assert trace["cleanup"] == {
        "server_reaped": True,
        "server_returncode": 0,
    }
    assert "failure" not in trace
    serialized_trace = trace_path.read_text(encoding="utf-8")
    assert "<allowed-repo>" in serialized_trace
    assert "<outside-repo>" in serialized_trace
    assert str(ROOT) not in serialized_trace

    responses = {
        event["target_action_id"]: event
        for event in trace["normalized_events"]
        if event["kind"] == "response"
    }
    for action_id in (
        "initialize",
        "tools-list",
        "create-branch",
        "list-branches",
    ):
        assert responses[action_id]["outcome"] == "success"
    assert _tool_rejected(responses["create-outside"])

    acceptance = _load(artifacts / "acceptance.json")
    git = acceptance["targets"]["git"]
    assert acceptance["slice"] == "git-application-server"
    assert git["package"] == {
        "ecosystem": "pypi",
        "name": GIT_PACKAGE,
        "sdist_sha256": GIT_SDIST_SHA256,
        "version": GIT_VERSION,
        "wheel_sha256": GIT_WHEEL_SHA256,
    }
    assert git["runs"] == {
        "attempted": 10,
        "byte_identical": True,
        "passed": 10,
    }
    assert git["state"] == {
        "allowed_branch_created": 10,
        "outside_branch_absent": 10,
        "outside_branch_rejected": 10,
        "outside_worktree_clean": 10,
    }
    assert git["trace"] == {
        "file": trace_path.name,
        "sha256": _sha256(trace_path),
        "sha256_counts": {_sha256(trace_path): 10},
    }
    assert git["lock_sha256"] == _sha256(benchmark / "uv.lock")


@pytest.mark.parametrize(
    "runner,program,target",
    (
        (run_m5_filesystem_acceptance, "run_m5_filesystem_acceptance.py", "filesystem"),
        (run_m5_git_acceptance, "run_m5_git_acceptance.py", "git"),
    ),
)
def test_m52_check_without_output_does_not_rewrite_checked_evidence(
    runner: object,
    program: str,
    target: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(output: Path, *, check: bool) -> dict[str, object]:
        captured.update(output=output, check=check)
        return {
            "targets": {
                target: {
                    "runs": {"attempted": 10, "passed": 10},
                }
            }
        }

    monkeypatch.setattr(runner, "run", run)
    monkeypatch.setattr(sys, "argv", [program, "--check"])

    assert runner.main() == 0
    assert captured["check"] is True
    assert captured["output"] != runner.DEFAULT_OUTPUT
    assert not Path(captured["output"]).exists()


def test_m52_git_server_environment_excludes_host_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "agent-socket")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")

    environment = run_m5_git_acceptance._git_environment(tmp_path, "git")

    assert "GITHUB_TOKEN" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "never"
    assert environment["HOME"] == str(tmp_path / "home")


def test_m52_path_normalization_covers_raw_and_resolved_spellings(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "parent" / ".." / "sandbox"

    normalized = run_m5_filesystem_acceptance._replace_paths(
        {
            "raw": str(sandbox / "state.txt"),
            "resolved": str(sandbox.resolve() / "state.txt"),
        },
        {sandbox: "<sandbox>"},
    )

    assert normalized == {
        "raw": "<sandbox>/state.txt",
        "resolved": "<sandbox>/state.txt",
    }
