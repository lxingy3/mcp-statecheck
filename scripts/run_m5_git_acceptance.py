"""Run the pinned M5 Git server acceptance slice."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

import anyio

from mcp_statecheck import __version__
from mcp_statecheck.execution import ExecutionResult, execute_stdio
from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.trace import TraceRecorder

if __package__:
    from .m5_application_recipes import (
        GIT_RECIPE_ID,
        ApplicationRecipe,
        load_application_recipe,
    )
    from .run_m4_acceptance import AcceptanceError, _atomic_write, _run
    from .run_m5_external_canary import (
        _isolated_environment,
        _load_object,
        _runtime_version,
        _sha256,
    )
    from .run_m5_filesystem_acceptance import (
        _canonical_work_root,
        _publish_trace,
        _replace_paths,
        _text,
    )
else:
    from m5_application_recipes import (
        GIT_RECIPE_ID,
        ApplicationRecipe,
        load_application_recipe,
    )
    from run_m4_acceptance import AcceptanceError, _atomic_write, _run
    from run_m5_external_canary import (
        _isolated_environment,
        _load_object,
        _runtime_version,
        _sha256,
    )
    from run_m5_filesystem_acceptance import (
        _canonical_work_root,
        _publish_trace,
        _replace_paths,
        _text,
    )

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "external" / "server-git"
CHECKED_ARTIFACTS = ROOT / "artifacts" / "m5" / "git"
DEFAULT_OUTPUT = Path("artifacts/m5/git")
CHECK_OUTPUT = Path("artifacts/tmp/m5/git")
PACKAGE = "mcp-server-git"
VERSION = "2026.8.18"
WHEEL_SHA256 = "6c32a8e771564122a9bafac373cf871fb3ab540ddc1ba0ee8e9c8c6e9878aef7"
SDIST_SHA256 = "96894ca661cfda45174a8882d4ca97d1f5261301c72ccad006ff04f4108f173f"
PROTOCOL_VERSION = "2025-11-25"
PYTHON_VERSION = "3.12.13"
RUNS = 10
TRACE_NAME = f"git-{VERSION}-stdio.json"
BRANCH = "m5-state"
OUTSIDE_BRANCH = "escape-attempt"
STATE_CONTENT = "alpha\nbeta\n"
COMMIT_MESSAGE = "record deterministic state"
SERVER_COMMIT_DATE = "2000-01-02 00:00:00 +0000"
SERVER_TIMEOUT_SECONDS = 30
TIMEOUT_SECONDS = 45


def _load_toml(path: Path, *, label: str) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AcceptanceError(f"{label} is not valid TOML") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} must be a TOML table")
    return value


def _application_recipe() -> ApplicationRecipe:
    try:
        return load_application_recipe(
            BENCHMARK / "recipe.json",
            target="git",
            target_version=VERSION,
        )
    except ValueError as exc:
        raise AcceptanceError(f"Git target recipe is invalid: {exc}") from exc


def _locked_target() -> tuple[str, ApplicationRecipe]:
    recipe = _application_recipe()
    manifest = _load_toml(BENCHMARK / "pyproject.toml", label="Git manifest")
    lock = _load_toml(BENCHMARK / "uv.lock", label="Git lock")
    if manifest.get("project") != {
        "name": "mcp-statecheck-server-git-benchmark",
        "version": "0.0.0",
        "requires-python": ">=3.12,<3.13",
        "dependencies": [f"{PACKAGE}=={VERSION}"],
    }:
        raise AcceptanceError("Git package version is not exactly pinned")
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise AcceptanceError("Git lock is missing its package list")
    target = next(
        (
            package
            for package in packages
            if isinstance(package, Mapping) and package.get("name") == PACKAGE
        ),
        None,
    )
    wheels = target.get("wheels") if isinstance(target, Mapping) else None
    if not isinstance(wheels, list):
        raise AcceptanceError("Git target lock is missing wheel metadata")
    wheel_hashes = {wheel.get("hash") for wheel in wheels if isinstance(wheel, Mapping)}
    sdist = target.get("sdist") if isinstance(target, Mapping) else None
    if (
        not isinstance(target, Mapping)
        or target.get("version") != VERSION
        or target.get("source") != {"registry": "https://pypi.org/simple"}
        or not isinstance(sdist, Mapping)
        or sdist.get("hash") != f"sha256:{SDIST_SHA256}"
        or wheel_hashes != {f"sha256:{WHEEL_SHA256}"}
    ):
        raise AcceptanceError("Git target metadata does not match the pinned release")
    return _sha256(BENCHMARK / "uv.lock"), recipe


def _actions(
    allowed: Path,
    outside: Path,
    recipe: ApplicationRecipe,
) -> tuple[Action, ...]:
    if recipe.recipe_id != GIT_RECIPE_ID:
        raise AcceptanceError("Git target recipe has no fixed action plan")
    calls = (
        ("status-dirty", 3, "git_status", {"repo_path": str(allowed)}),
        (
            "add-file",
            4,
            "git_add",
            {"repo_path": str(allowed), "files": ["state.txt"]},
        ),
        (
            "diff-staged",
            5,
            "git_diff_staged",
            {"repo_path": str(allowed), "context_lines": 3},
        ),
        (
            "commit-change",
            6,
            "git_commit",
            {"repo_path": str(allowed), "message": COMMIT_MESSAGE},
        ),
        (
            "log-history",
            7,
            "git_log",
            {"repo_path": str(allowed), "max_count": 2},
        ),
        ("status-clean", 8, "git_status", {"repo_path": str(allowed)}),
        (
            "create-branch",
            9,
            "git_create_branch",
            {"repo_path": str(allowed), "branch_name": BRANCH, "base_branch": "main"},
        ),
        (
            "create-outside",
            10,
            "git_create_branch",
            {
                "repo_path": str(outside),
                "branch_name": OUTSIDE_BRANCH,
                "base_branch": "main",
            },
        ),
        (
            "list-branches",
            11,
            "git_branch",
            {"repo_path": str(allowed), "branch_type": "local"},
        ),
    )
    actions = [
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version=PROTOCOL_VERSION,
            capabilities={},
        ),
        Action(
            "initialize-response", ActionKind.RESPONSE, target_action_id="initialize"
        ),
        Action("initialized", ActionKind.INITIALIZED),
        Action(
            "tools-list",
            ActionKind.REQUEST,
            mcp_request_id=2,
            method="tools/list",
            payload={},
        ),
        Action(
            "tools-list-response", ActionKind.RESPONSE, target_action_id="tools-list"
        ),
    ]
    for action_id, request_id, name, arguments in calls:
        actions.extend(
            (
                Action(
                    action_id,
                    ActionKind.REQUEST,
                    mcp_request_id=request_id,
                    method="tools/call",
                    payload={"name": name, "arguments": arguments},
                ),
                Action(
                    f"{action_id}-response",
                    ActionKind.RESPONSE,
                    target_action_id=action_id,
                ),
            )
        )
    return tuple(actions)


def _canonicalize_status_event(value: Mapping[str, object]) -> Mapping[str, object]:
    target = value.get("target_action_id")
    status = {
        "status-dirty": "Repository status:\n<dirty: state.txt>",
        "status-clean": "Repository status:\n<clean>",
    }.get(target)
    if value.get("kind") != "response" or status is None:
        return value
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        return value
    content = payload.get("content")
    if not isinstance(content, list):
        return value
    normalized_content: list[object] = []
    for item in content:
        if isinstance(item, Mapping) and isinstance(item.get("text"), str):
            normalized_content.append({**item, "text": status})
        else:
            normalized_content.append(item)
    return {**value, "payload": {**payload, "content": normalized_content}}


def _response(result: ExecutionResult, action_id: str) -> Mapping[str, object]:
    for event in result.events:
        if (
            event.get("kind") == "response"
            and event.get("target_action_id") == action_id
        ):
            return event
    raise AcceptanceError(f"Git trace is missing the {action_id} response")


def _payload(response: Mapping[str, object], action_id: str) -> Mapping[str, object]:
    if response.get("outcome") != "success" or not isinstance(
        response.get("payload"), Mapping
    ):
        raise AcceptanceError(f"Git {action_id} did not return a result object")
    return response["payload"]  # type: ignore[return-value]


def _tool_rejected(response: Mapping[str, object]) -> bool:
    if response.get("outcome") == "error":
        return True
    payload = response.get("payload")
    return isinstance(payload, Mapping) and payload.get("isError") is True


def _trace_commit_oid(trace: Path) -> str:
    artifact = _load_object(trace, label="Git trace")
    events = artifact.get("normalized_events")
    responses = (
        [
            event
            for event in events
            if isinstance(event, Mapping)
            and event.get("kind") == "response"
            and event.get("target_action_id") == "commit-change"
        ]
        if isinstance(events, list)
        else []
    )
    if len(responses) != 1:
        raise AcceptanceError("Git trace must contain one commit response")
    payload = _payload(responses[0], "commit-change")
    matches = re.findall(r"\b[0-9a-f]{40}\b", _text(payload))
    if len(matches) != 1:
        raise AcceptanceError("Git trace commit response must contain one object ID")
    return matches[0]


async def _probe_git(
    *, python: Path, allowed: Path, outside: Path, trace: Path
) -> None:
    recipe = _application_recipe()
    actions = _actions(allowed, outside, recipe)
    result = await execute_stdio(
        actions,
        (
            str(python),
            "-m",
            "mcp_server_git",
            "--repository",
            str(allowed),
        ),
        timeout=SERVER_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise AcceptanceError(f"Git server exited with status {result.returncode}")

    initialize = _payload(_response(result, "initialize"), "initialize")
    if initialize.get("protocolVersion") != PROTOCOL_VERSION:
        raise AcceptanceError("Git server negotiated an unexpected protocol version")
    tools_payload = _payload(_response(result, "tools-list"), "tools-list")
    tools = tools_payload.get("tools")
    names = (
        {tool.get("name") for tool in tools if isinstance(tool, Mapping)}
        if isinstance(tools, list)
        else set()
    )
    if not {
        "git_add",
        "git_branch",
        "git_commit",
        "git_create_branch",
        "git_diff_staged",
        "git_log",
        "git_status",
    }.issubset(names):
        raise AcceptanceError("Git server is missing required tools")
    dirty_payload = _payload(_response(result, "status-dirty"), "status-dirty")
    add_payload = _payload(_response(result, "add-file"), "add-file")
    diff_payload = _payload(_response(result, "diff-staged"), "diff-staged")
    commit_payload = _payload(_response(result, "commit-change"), "commit-change")
    log_payload = _payload(_response(result, "log-history"), "log-history")
    clean_payload = _payload(_response(result, "status-clean"), "status-clean")
    create_payload = _payload(_response(result, "create-branch"), "create-branch")
    list_payload = _payload(_response(result, "list-branches"), "list-branches")
    dirty_text = _text(dirty_payload)
    if "state.txt" not in dirty_text or "working tree clean" in dirty_text:
        raise AcceptanceError("Git server did not observe the dirty working tree")
    if add_payload.get("isError") is True or "Files staged successfully" not in _text(
        add_payload
    ):
        raise AcceptanceError("Git add state transition failed")
    diff_text = _text(diff_payload)
    if not {
        "diff --git a/state.txt b/state.txt",
        "+alpha",
        "+beta",
    }.issubset(diff_text.splitlines()):
        raise AcceptanceError("Git staged diff did not contain the fixture change")
    commit_text = _text(commit_payload)
    commit_match = re.search(r"\b[0-9a-f]{40}\b", commit_text)
    if commit_payload.get("isError") is True or commit_match is None:
        raise AcceptanceError("Git commit did not return a commit object ID")
    commit_oid = commit_match.group()
    log_text = _text(log_payload)
    log_entries = log_text.split("\n\n")
    if not any(
        f"Commit: '{commit_oid}'" in entry and f"Message: '{COMMIT_MESSAGE}'" in entry
        for entry in log_entries
    ):
        raise AcceptanceError("Git log did not contain the new commit")
    if "nothing to commit, working tree clean" not in _text(clean_payload):
        raise AcceptanceError("Git server did not observe a clean working tree")
    if create_payload.get("isError") is True or BRANCH not in str(list_payload):
        raise AcceptanceError("Git branch state transition failed")
    if not _tool_rejected(_response(result, "create-outside")):
        raise AcceptanceError("Git server accepted an outside repository mutation")

    aliases = {allowed: "<allowed-repo>", outside: "<outside-repo>"}
    recorder = TraceRecorder(
        protocol_version=PROTOCOL_VERSION,
        adapter="wire",
        sdk_version=f"{PACKAGE}=={VERSION}",
        transport="stdio",
        seed=0,
        fixture_id="git-repository-state",
        cleanup={"server_reaped": True, "server_returncode": result.returncode},
        generation={
            "engine": "mcp-statecheck M5 acceptance",
            "outcome": "passed",
            "profile": "application-state",
        },
        target_recipe=recipe.target_recipe,
    )
    for action in actions:
        normalized = _replace_paths(action.to_dict(), aliases)
        assert isinstance(normalized, Mapping)
        recorder.record_action(normalized)
    for event in result.events:
        normalized = _replace_paths(event, aliases)
        assert isinstance(normalized, Mapping)
        normalized = _canonicalize_status_event(normalized)
        recorder.record_event(normalized)
    recorder.write(trace)


def _probe_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--allowed", required=True, type=Path)
    parser.add_argument("--outside", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        anyio.run(
            lambda: _probe_git(
                python=args.python,
                allowed=args.allowed,
                outside=args.outside,
                trace=args.trace,
            )
        )
    except (AcceptanceError, OSError, ValueError) as exc:
        print(f"Git probe failed: {exc}", file=sys.stderr)
        return 2
    return 0


def _git_environment(work: Path, git: str) -> dict[str, str]:
    environment = _isolated_environment(work)
    environment.update(
        {
            "GCM_INTERACTIVE": "never",
            "GIT_CONFIG_GLOBAL": str(work / "home" / ".gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_PYTHON_GIT_EXECUTABLE": git,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_DATE": SERVER_COMMIT_DATE,
            "GIT_COMMITTER_DATE": SERVER_COMMIT_DATE,
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
            "UV_CACHE_DIR": str(work / "uv-cache"),
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )
    return environment


def _acceptance_python() -> Path:
    version = platform.python_version()
    if version != PYTHON_VERSION:
        raise AcceptanceError(
            f"Git acceptance requires Python {PYTHON_VERSION}, found {version}"
        )
    return Path(sys.executable).resolve()


def _init_repo(repo: Path, *, git: str, environment: Mapping[str, str]) -> str:
    repo.mkdir(parents=True)
    _run(
        [git, "init", "--initial-branch=main"],
        cwd=repo,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="Git fixture init",
    )
    for key, value in (
        ("core.autocrlf", "false"),
        ("core.filemode", "false"),
        ("user.name", "mcp-statecheck fixture"),
        ("user.email", "fixture@mcp-statecheck.invalid"),
    ):
        _run(
            [git, "config", key, value],
            cwd=repo,
            environment=environment,
            timeout=TIMEOUT_SECONDS,
            label=f"Git fixture config {key}",
        )
    (repo / "README.md").write_bytes(b"mcp-statecheck git acceptance\n")
    _run(
        [git, "add", "README.md"],
        cwd=repo,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="Git fixture add",
    )
    commit_environment = dict(environment)
    commit_environment.update(
        {
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        }
    )
    _run(
        [git, "commit", "--message", "initial fixture"],
        cwd=repo,
        environment=commit_environment,
        timeout=TIMEOUT_SECONDS,
        label="Git fixture commit",
    )
    return _run(
        [git, "rev-parse", "HEAD"],
        cwd=repo,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="Git fixture HEAD probe",
    ).stdout.strip()


def _verify_repos(
    allowed: Path,
    outside: Path,
    *,
    initial_oid: str,
    git: str,
    environment: Mapping[str, str],
) -> str:
    head = _run(
        [git, "rev-parse", "HEAD"],
        cwd=allowed,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="allowed Git HEAD probe",
    ).stdout.strip()
    allowed_branch = _run(
        [git, "rev-parse", f"refs/heads/{BRANCH}"],
        cwd=allowed,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="allowed Git branch probe",
    ).stdout.strip()
    main_branch = _run(
        [git, "rev-parse", "refs/heads/main"],
        cwd=allowed,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="allowed Git main probe",
    ).stdout.strip()
    if allowed_branch != main_branch or main_branch != head:
        raise AcceptanceError("Git branch does not point to the recipe commit")
    parent = _run(
        [git, "rev-parse", "HEAD^"],
        cwd=allowed,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="allowed Git parent probe",
    ).stdout.strip()
    if parent != initial_oid:
        raise AcceptanceError("Git recipe commit has an unexpected parent")
    commit_count = _run(
        [git, "rev-list", "--count", "HEAD"],
        cwd=allowed,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="allowed Git history probe",
    ).stdout.strip()
    commit_metadata = _run(
        [git, "show", "-s", "--format=%s%n%an%n%ae%n%cn%n%ce%n%at%n%ct", "HEAD"],
        cwd=allowed,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="allowed Git commit metadata probe",
    ).stdout.splitlines()
    if commit_count != "2" or commit_metadata != [
        COMMIT_MESSAGE,
        "mcp-statecheck fixture",
        "fixture@mcp-statecheck.invalid",
        "mcp-statecheck fixture",
        "fixture@mcp-statecheck.invalid",
        "946771200",
        "946771200",
    ]:
        raise AcceptanceError(
            "Git recipe commit metadata is not deterministic: "
            f"count={commit_count!r}, metadata={commit_metadata!r}"
        )
    committed_state = _run(
        [git, "show", "HEAD:state.txt"],
        cwd=allowed,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="allowed Git committed state probe",
    ).stdout
    committed_readme = _run(
        [git, "show", "HEAD:README.md"],
        cwd=allowed,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="allowed Git committed README probe",
    ).stdout
    changed_paths = _run(
        [git, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD^", "HEAD"],
        cwd=allowed,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="allowed Git changed paths probe",
    ).stdout.splitlines()
    if (
        committed_state != STATE_CONTENT
        or committed_readme != "mcp-statecheck git acceptance\n"
        or changed_paths != ["state.txt"]
    ):
        raise AcceptanceError("Git recipe commit contents are incorrect")
    if (allowed / "state.txt").read_bytes() != STATE_CONTENT.encode():
        raise AcceptanceError("Git recipe commit content is incorrect")
    tree = _run(
        [git, "ls-tree", "--name-only", "HEAD"],
        cwd=allowed,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="allowed Git tree probe",
    ).stdout.splitlines()
    if set(tree) != {"README.md", "state.txt"}:
        raise AcceptanceError("Git recipe commit tree is incorrect")
    outside_probe = _run(
        [git, "rev-parse", "--verify", "--quiet", f"refs/heads/{OUTSIDE_BRANCH}"],
        cwd=outside,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="outside Git branch probe",
        expected=(1,),
    )
    if outside_probe.stdout or outside_probe.stderr:
        raise AcceptanceError("outside Git branch probe emitted unexpected output")
    outside_head = _run(
        [git, "rev-parse", "HEAD"],
        cwd=outside,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="outside Git HEAD probe",
    ).stdout.strip()
    outside_count = _run(
        [git, "rev-list", "--count", "HEAD"],
        cwd=outside,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label="outside Git history probe",
    ).stdout.strip()
    if outside_head != initial_oid or outside_count != "1":
        raise AcceptanceError("outside Git history changed")
    for repo, label in ((allowed, "allowed"), (outside, "outside")):
        status = _run(
            [git, "status", "--porcelain=v1"],
            cwd=repo,
            environment=environment,
            timeout=TIMEOUT_SECONDS,
            label=f"{label} Git status probe",
        ).stdout
        if status:
            raise AcceptanceError(f"{label} Git fixture is not clean")
    return head


def run(output: Path, *, check: bool) -> dict[str, object]:
    lock_sha256, recipe = _locked_target()
    uv = shutil.which("uv")
    git = shutil.which("git")
    if uv is None or git is None:
        raise AcceptanceError("uv and Git are required for Git server acceptance")
    acceptance_python = _acceptance_python()

    with tempfile.TemporaryDirectory(prefix="mcp-statecheck-m5-git-") as temporary:
        work = _canonical_work_root(temporary)
        environment = _git_environment(work, git)
        install = work / "install"
        install.mkdir()
        shutil.copy2(BENCHMARK / "pyproject.toml", install / "pyproject.toml")
        shutil.copy2(BENCHMARK / "uv.lock", install / "uv.lock")
        _run(
            [
                uv,
                "sync",
                "--project",
                install,
                "--locked",
                "--no-dev",
                "--no-build",
                "--no-install-project",
                "--python",
                acceptance_python,
            ],
            cwd=work,
            environment=environment,
            timeout=180,
            label="locked Git server install",
        )
        if _sha256(install / "uv.lock") != lock_sha256:
            raise AcceptanceError("uv changed the copied Git lock")
        python = (
            install
            / ".venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        if not python.is_file():
            raise AcceptanceError(
                "Git server environment is missing its Python executable"
            )
        python_version = _runtime_version(
            [python, "-c", "import platform; print(platform.python_version())"],
            cwd=work,
            environment=environment,
            label="Git server Python version probe",
        )
        package_version = _runtime_version(
            [
                python,
                "-c",
                "from importlib.metadata import version; print(version('mcp-server-git'))",
            ],
            cwd=work,
            environment=environment,
            label="Git server package version probe",
        )
        if python_version != PYTHON_VERSION or package_version != VERSION:
            raise AcceptanceError(
                "Git server runtime does not match the pinned versions"
            )
        git_version = _runtime_version(
            [git, "--version"],
            cwd=work,
            environment=environment,
            label="Git version probe",
        )

        traces = work / "traces"
        traces.mkdir()
        trace_paths: list[Path] = []
        commit_oids: list[str] = []
        for attempt in range(1, RUNS + 1):
            case = work / "cases" / f"run-{attempt:02d}"
            allowed = case / "allowed"
            outside = case / "outside"
            allowed_initial = _init_repo(allowed, git=git, environment=environment)
            outside_initial = _init_repo(outside, git=git, environment=environment)
            if allowed_initial != outside_initial:
                raise AcceptanceError("Git fixture commits were not deterministic")
            (allowed / "state.txt").write_bytes(STATE_CONTENT.encode())
            trace = traces / f"run-{attempt:02d}.json"
            _run(
                [
                    sys.executable,
                    Path(__file__).resolve(),
                    "_probe-git",
                    "--python",
                    python,
                    "--allowed",
                    allowed,
                    "--outside",
                    outside,
                    "--trace",
                    trace,
                ],
                cwd=case,
                environment=environment,
                timeout=TIMEOUT_SECONDS,
                label=f"Git acceptance run {attempt}/{RUNS}",
            )
            commit_oid = _verify_repos(
                allowed,
                outside,
                initial_oid=allowed_initial,
                git=git,
                environment=environment,
            )
            if _trace_commit_oid(trace) != commit_oid:
                raise AcceptanceError("Git trace commit does not match repository HEAD")
            commit_oids.append(commit_oid)
            trace_paths.append(trace)

        first = trace_paths[0].read_bytes()
        if any(path.read_bytes() != first for path in trace_paths[1:]):
            raise AcceptanceError("Git traces were not byte-identical")
        if _application_recipe() != recipe:
            raise AcceptanceError("Git target recipe changed during acceptance")
        oid_counts = {oid: commit_oids.count(oid) for oid in set(commit_oids)}
        if len(oid_counts) != 1:
            raise AcceptanceError("Git recipe commits were not deterministic")
        commit_oid = commit_oids[0]
        trace_sha256 = hashlib.sha256(first).hexdigest()
        _publish_trace(
            trace_paths[0],
            output / TRACE_NAME,
            checked=CHECKED_ARTIFACTS / TRACE_NAME,
            check=check,
            label="Git",
        )
        summary: dict[str, object] = {
            "schema_version": 1,
            "milestone": "M5",
            "slice": "git-application-server",
            "status": "passed",
            "protocol_version": PROTOCOL_VERSION,
            "transport": "stdio",
            "targets": {
                "git": {
                    "package": {
                        "ecosystem": "pypi",
                        "name": PACKAGE,
                        "version": VERSION,
                        "wheel_sha256": WHEEL_SHA256,
                        "sdist_sha256": SDIST_SHA256,
                    },
                    "runtime": {
                        "name": "python",
                        "version": python_version,
                        "git": git_version,
                    },
                    "runs": {
                        "attempted": RUNS,
                        "passed": RUNS,
                        "byte_identical": True,
                    },
                    "target_recipe": recipe.target_recipe,
                    "target_recipe_sha256": recipe.sha256,
                    "state": {
                        "allowed_branch_created": RUNS,
                        "allowed_worktree_clean": RUNS,
                        "dirty_status_verified": RUNS,
                        "file_staged": RUNS,
                        "staged_diff_verified": RUNS,
                        "commit_created": RUNS,
                        "commit_oid_matches_head": RUNS,
                        "commit_parent_verified": RUNS,
                        "commit_contents_verified": RUNS,
                        "commit_log_verified": RUNS,
                        "clean_status_verified": RUNS,
                        "outside_branch_rejected": RUNS,
                        "outside_branch_absent": RUNS,
                        "outside_worktree_clean": RUNS,
                        "outside_history_unchanged": RUNS,
                    },
                    "commit": {
                        "message": COMMIT_MESSAGE,
                        "oid": commit_oid,
                        "oid_counts": oid_counts,
                    },
                    "cleanup": {
                        "server_reaped": RUNS,
                        "server_returncode_zero": RUNS,
                    },
                    "trace": {
                        "file": TRACE_NAME,
                        "sha256": trace_sha256,
                        "sha256_counts": {trace_sha256: RUNS},
                    },
                    "lock_sha256": lock_sha256,
                }
            },
            "runtime": {
                "mcp_statecheck": __version__,
                "python": platform.python_version(),
                "uv": _runtime_version(
                    [uv, "--version"],
                    cwd=work,
                    environment=environment,
                    label="uv version probe",
                ),
            },
            "isolation": {
                "ambient_credentials_removed": True,
                "allowed_repository": "fresh temporary repository per run",
                "repository_flag_required": True,
                "os_sandbox": False,
            },
        }
        _atomic_write(output / "acceptance.json", summary)
        return summary


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_probe-git":
        return _probe_main(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description="Run the pinned Git server acceptance ten times."
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the generated trace with the checked-in M5 evidence",
    )
    args = parser.parse_args()
    try:
        output = args.output or (CHECK_OUTPUT if args.check else DEFAULT_OUTPUT)
        summary = run(output, check=args.check)
        destination = (
            f"checked-in evidence; output {output}"
            if args.check and args.output is None
            else str(output)
        )
    except (AcceptanceError, OSError) as exc:
        print(f"M5 Git acceptance failed: {exc}", file=sys.stderr)
        return 2
    git_target = summary["targets"]["git"]  # type: ignore[index]
    print(
        "M5 Git acceptance passed: "
        f"{git_target['runs']['passed']}/{git_target['runs']['attempted']} "  # type: ignore[index]
        f"byte-identical runs; {destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
