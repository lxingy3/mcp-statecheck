import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

import anyio
import pytest

from mcp_statecheck.replay import ReplayInfrastructureError, replay_artifact
from scripts import (
    m5_application_recipes,
    run_m5_filesystem_acceptance,
    run_m5_git_acceptance,
)

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


def _tool_calls(
    trace: dict[str, object],
) -> list[tuple[str, str, dict[str, object]]]:
    actions = trace["canonical_actions"]
    assert isinstance(actions, list)
    calls: list[tuple[str, str, dict[str, object]]] = []
    for action in actions:
        if not isinstance(action, dict) or action.get("method") != "tools/call":
            continue
        payload = action["payload"]
        assert isinstance(payload, dict)
        arguments = payload["arguments"]
        assert isinstance(arguments, dict)
        calls.append((action["action_id"], payload["name"], arguments))
    return calls


@pytest.mark.parametrize(
    "target,target_version,recipe_id",
    (
        (
            "filesystem",
            FILESYSTEM_VERSION,
            "filesystem-2026.7.10-stdio-write-edit-boundary",
        ),
        ("git", GIT_VERSION, "git-2026.8.18-stdio-stage-commit-boundary"),
    ),
)
def test_m53_application_recipes_are_versioned_and_allowlisted(
    target: str,
    target_version: str,
    recipe_id: str,
) -> None:
    path = ROOT / "benchmarks" / "external" / f"server-{target}" / "recipe.json"

    recipe = m5_application_recipes.load_application_recipe(
        path,
        target=target,
        target_version=target_version,
    )

    assert recipe.target_recipe == {
        "kind": "application-state",
        "recipe_id": recipe_id,
        "version": 2,
    }
    assert recipe.sha256 == _sha256(path)


def test_m53_application_recipe_is_bound_to_the_target_release() -> None:
    path = ROOT / "benchmarks" / "external" / "server-filesystem" / "recipe.json"

    with pytest.raises(ValueError, match="not allowlisted"):
        m5_application_recipes.load_application_recipe(
            path,
            target="filesystem",
            target_version="2026.7.9",
        )


def test_m53_application_recipe_rejects_executable_or_unknown_input(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recipe.json"
    sentinel = tmp_path / "executed.txt"
    invalid_recipes = (
        {
            "kind": "application-state",
            "recipe_id": "filesystem-2026.7.10-stdio-write-edit-boundary",
            "version": 2,
            "command": ["powershell", "-Command", f"Set-Content {sentinel} bad"],
        },
        {
            "kind": "application-state",
            "recipe_id": "filesystem-2026.7.10-stdio-write-edit-boundary",
            "version": True,
        },
        {
            "kind": "application-state",
            "recipe_id": "not-allowlisted",
            "version": 2,
        },
    )

    for value in invalid_recipes:
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ValueError):
            m5_application_recipes.load_application_recipe(
                path,
                target="filesystem",
                target_version=FILESYSTEM_VERSION,
            )

    assert not sentinel.exists()


@pytest.mark.parametrize(
    "payload",
    (
        b'{"kind":"application-state","kind":"application-state",'
        b'"recipe_id":"filesystem-2026.7.10-stdio-write-edit-boundary",'
        b'"version":2}',
        b'{"kind":"application-state",'
        b'"recipe_id":"filesystem-2026.7.10-stdio-write-edit-boundary",'
        b'"version":NaN}',
    ),
)
def test_m53_application_recipe_rejects_ambiguous_json(
    tmp_path: Path,
    payload: bytes,
) -> None:
    path = tmp_path / "recipe.json"
    path.write_bytes(payload)

    with pytest.raises(ValueError, match="not valid JSON"):
        m5_application_recipes.load_application_recipe(
            path,
            target="filesystem",
            target_version=FILESYSTEM_VERSION,
        )


@pytest.mark.parametrize(
    "runner,label",
    (
        (run_m5_filesystem_acceptance, "Filesystem"),
        (run_m5_git_acceptance, "Git"),
    ),
)
def test_m53_invalid_recipe_is_rejected_before_tool_discovery(
    runner: object,
    label: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark = tmp_path / "benchmark"
    benchmark.mkdir()
    (benchmark / "recipe.json").write_text(
        json.dumps(
            {
                "kind": "application-state",
                "recipe_id": "not-allowlisted",
                "version": 2,
                "command": ["untrusted"],
            }
        ),
        encoding="utf-8",
    )

    def unexpected_discovery(_: str) -> str:
        pytest.fail("external tool discovery ran before recipe validation")

    monkeypatch.setattr(runner, "BENCHMARK", benchmark)
    monkeypatch.setattr(runner.shutil, "which", unexpected_discovery)

    with pytest.raises(runner.AcceptanceError, match=f"{label} target recipe"):
        runner.run(tmp_path / "output", check=False)


@pytest.mark.parametrize(
    "trace",
    (
        ARTIFACTS / f"filesystem-{FILESYSTEM_VERSION}-stdio.json",
        ROOT / "artifacts" / "m5" / "git" / f"git-{GIT_VERSION}-stdio.json",
    ),
)
def test_m53_application_recipes_are_not_public_replay_inputs(trace: Path) -> None:
    with pytest.raises(
        ReplayInfrastructureError,
        match="target_recipe fields",
    ):
        anyio.run(replay_artifact, trace, 1, 5)


def test_m53_filesystem_recipe_verifies_edit_list_and_boundary_state() -> None:
    benchmark = ROOT / "benchmarks" / "external" / "server-filesystem"
    recipe_path = benchmark / "recipe.json"
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
    assert trace["target_recipe"] == {
        "kind": "application-state",
        "recipe_id": "filesystem-2026.7.10-stdio-write-edit-boundary",
        "version": 2,
    }
    assert _tool_calls(trace) == [
        ("list-allowed", "list_allowed_directories", {}),
        (
            "write-file",
            "write_file",
            {"content": "alpha\nbeta\n", "path": "<sandbox>/state.txt"},
        ),
        (
            "edit-file",
            "edit_file",
            {
                "dryRun": False,
                "edits": [{"newText": "beta-updated", "oldText": "beta"}],
                "path": "<sandbox>/state.txt",
            },
        ),
        ("read-file", "read_text_file", {"path": "<sandbox>/state.txt"}),
        ("list-directory", "list_directory", {"path": "<sandbox>"}),
        (
            "write-outside",
            "write_file",
            {"content": "overwritten\n", "path": "<outside>/sentinel.txt"},
        ),
        (
            "edit-outside",
            "edit_file",
            {
                "dryRun": False,
                "edits": [{"newText": "overwritten", "oldText": "outside"}],
                "path": "<outside>/sentinel.txt",
            },
        ),
        ("list-outside", "list_directory", {"path": "<outside>"}),
        (
            "read-after-rejections",
            "read_text_file",
            {"path": "<sandbox>/state.txt"},
        ),
    ]
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
        "list-allowed",
        "write-file",
        "edit-file",
        "read-file",
        "list-directory",
        "write-outside",
        "edit-outside",
        "list-outside",
        "read-after-rejections",
    ):
        assert responses[action_id]["outcome"] == "success"
    assert "Allowed directories:\n<sandbox>" in _tool_text(responses["list-allowed"])
    assert "beta-updated" in _tool_text(responses["edit-file"])
    assert "alpha\nbeta-updated\n" in _tool_text(responses["read-file"])
    assert "[FILE] state.txt" in _tool_text(responses["list-directory"])
    assert "alpha\nbeta-updated\n" in _tool_text(responses["read-after-rejections"])
    for action_id in ("write-outside", "edit-outside", "list-outside"):
        assert _tool_rejected(responses[action_id])

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
    assert filesystem["target_recipe"] == trace["target_recipe"]
    assert filesystem["target_recipe_sha256"] == _sha256(recipe_path)
    assert filesystem["state"] == {
        "allowed_directory_reported": 10,
        "allowed_edit_verified": 10,
        "allowed_list_verified": 10,
        "outside_sentinel_unchanged": 10,
        "outside_edit_rejected": 10,
        "outside_list_rejected": 10,
        "outside_write_rejected": 10,
        "post_rejection_read_verified": 10,
        "written_and_read_content_verified": 10,
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


def _tool_text(response: dict[str, object]) -> str:
    payload = response["payload"]
    assert isinstance(payload, dict)
    content = payload["content"]
    assert isinstance(content, list)
    return "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )


def test_m53_git_recipe_verifies_stage_commit_and_boundary_state() -> None:
    benchmark = ROOT / "benchmarks" / "external" / "server-git"
    recipe_path = benchmark / "recipe.json"
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
    assert trace["target_recipe"] == {
        "kind": "application-state",
        "recipe_id": "git-2026.8.18-stdio-stage-commit-boundary",
        "version": 2,
    }
    assert _tool_calls(trace) == [
        ("status-dirty", "git_status", {"repo_path": "<allowed-repo>"}),
        (
            "add-file",
            "git_add",
            {"files": ["state.txt"], "repo_path": "<allowed-repo>"},
        ),
        (
            "diff-staged",
            "git_diff_staged",
            {"context_lines": 3, "repo_path": "<allowed-repo>"},
        ),
        (
            "commit-change",
            "git_commit",
            {
                "message": "record deterministic state",
                "repo_path": "<allowed-repo>",
            },
        ),
        (
            "log-history",
            "git_log",
            {"max_count": 2, "repo_path": "<allowed-repo>"},
        ),
        ("status-clean", "git_status", {"repo_path": "<allowed-repo>"}),
        (
            "create-branch",
            "git_create_branch",
            {
                "base_branch": "main",
                "branch_name": "m5-state",
                "repo_path": "<allowed-repo>",
            },
        ),
        (
            "create-outside",
            "git_create_branch",
            {
                "base_branch": "main",
                "branch_name": "escape-attempt",
                "repo_path": "<outside-repo>",
            },
        ),
        (
            "list-branches",
            "git_branch",
            {"branch_type": "local", "repo_path": "<allowed-repo>"},
        ),
    ]
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
        "status-dirty",
        "add-file",
        "diff-staged",
        "commit-change",
        "log-history",
        "status-clean",
        "create-branch",
        "list-branches",
    ):
        assert responses[action_id]["outcome"] == "success"
    assert _tool_text(responses["status-dirty"]) == (
        "Repository status:\n<dirty: state.txt>"
    )
    assert "Files staged successfully" in _tool_text(responses["add-file"])
    staged_diff = _tool_text(responses["diff-staged"])
    assert "diff --git a/state.txt b/state.txt" in staged_diff
    assert "+alpha" in staged_diff
    assert "+beta" in staged_diff
    commit_text = _tool_text(responses["commit-change"])
    commit_match = re.search(r"\b[0-9a-f]{40}\b", commit_text)
    assert commit_match is not None
    commit_oid = commit_match.group()
    assert run_m5_git_acceptance._trace_commit_oid(trace_path) == commit_oid
    log_text = _tool_text(responses["log-history"])
    assert commit_oid in log_text
    assert "record deterministic state" in log_text
    assert _tool_text(responses["status-clean"]) == ("Repository status:\n<clean>")
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
    assert git["target_recipe"] == trace["target_recipe"]
    assert git["target_recipe_sha256"] == _sha256(recipe_path)
    assert git["state"] == {
        "allowed_branch_created": 10,
        "allowed_worktree_clean": 10,
        "clean_status_verified": 10,
        "commit_contents_verified": 10,
        "commit_created": 10,
        "commit_log_verified": 10,
        "commit_oid_matches_head": 10,
        "commit_parent_verified": 10,
        "dirty_status_verified": 10,
        "file_staged": 10,
        "outside_branch_absent": 10,
        "outside_branch_rejected": 10,
        "outside_history_unchanged": 10,
        "outside_worktree_clean": 10,
        "staged_diff_verified": 10,
    }
    assert git["commit"] == {
        "message": "record deterministic state",
        "oid": commit_oid,
        "oid_counts": {commit_oid: 10},
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
    assert captured["output"] == runner.CHECK_OUTPUT
    assert captured["output"] != runner.DEFAULT_OUTPUT


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
    assert environment["GIT_AUTHOR_DATE"] == "2000-01-02 00:00:00 +0000"
    assert environment["GIT_COMMITTER_DATE"] == "2000-01-02 00:00:00 +0000"
    assert environment["LANG"] == "C"
    assert environment["LC_ALL"] == "C"
    assert environment["TZ"] == "UTC"
    assert environment["HOME"] == str(tmp_path / "home")


def test_m52_git_acceptance_requires_the_current_exact_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_m5_git_acceptance._acceptance_python() == Path(sys.executable).resolve()

    monkeypatch.setattr(
        run_m5_git_acceptance.platform, "python_version", lambda: "3.12.12"
    )

    with pytest.raises(
        run_m5_git_acceptance.AcceptanceError,
        match="requires Python 3.12.13, found 3.12.12",
    ):
        run_m5_git_acceptance._acceptance_python()


def test_m52_trace_mismatch_preserves_observed_without_rewriting_golden(
    tmp_path: Path,
) -> None:
    observed = tmp_path / "observed.json"
    checked = tmp_path / "trace.json"
    observed.write_bytes(b'{"value":"observed"}\n')
    checked.write_bytes(b'{"value":"checked"}\n')

    with pytest.raises(
        run_m5_filesystem_acceptance.AcceptanceError,
        match="observed SHA-256",
    ):
        run_m5_filesystem_acceptance._publish_trace(
            observed,
            checked,
            checked=checked,
            check=True,
            label="Filesystem",
        )

    assert checked.read_bytes() == b'{"value":"checked"}\n'
    assert (tmp_path / "observed-trace.json").read_bytes() == observed.read_bytes()


def test_m52_missing_golden_preserves_observed_evidence(tmp_path: Path) -> None:
    observed = tmp_path / "source.json"
    checked = tmp_path / "trace.json"
    observed.write_bytes(b'{"value":"observed"}\n')

    with pytest.raises(
        run_m5_filesystem_acceptance.AcceptanceError,
        match="checked-in Filesystem trace is missing",
    ):
        run_m5_filesystem_acceptance._publish_trace(
            observed,
            checked,
            checked=checked,
            check=True,
            label="Filesystem",
        )

    assert not checked.exists()
    assert (tmp_path / "observed-trace.json").read_bytes() == observed.read_bytes()


def test_m52_matching_trace_uses_standard_output_name(tmp_path: Path) -> None:
    observed = tmp_path / "source.json"
    checked = tmp_path / "checked.json"
    output = tmp_path / "output" / "trace.json"
    observed.write_bytes(b'{"value":"same"}\n')
    checked.write_bytes(observed.read_bytes())
    diagnostic = output.parent / "observed-trace.json"
    diagnostic.parent.mkdir(parents=True)
    diagnostic.write_bytes(b"stale diagnostic\n")

    run_m5_filesystem_acceptance._publish_trace(
        observed,
        output,
        checked=checked,
        check=True,
        label="Filesystem",
    )

    assert output.read_bytes() == observed.read_bytes()
    assert not diagnostic.exists()


def test_m52_filesystem_work_root_is_canonical(tmp_path: Path) -> None:
    raw = tmp_path / "parent" / ".." / "work"

    canonical = run_m5_filesystem_acceptance._canonical_work_root(raw)

    assert canonical == raw.resolve()
    assert canonical == canonical.resolve()


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
