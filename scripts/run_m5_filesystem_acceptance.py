"""Run the pinned M5 Filesystem server acceptance slice."""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import anyio

from mcp_statecheck import __version__
from mcp_statecheck.execution import ExecutionResult, execute_stdio
from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.trace import TraceRecorder

if __package__:
    from .run_m4_acceptance import AcceptanceError, _atomic_write, _run
    from .run_m5_external_canary import (
        _copy_file,
        _isolated_environment,
        _load_object,
        _runtime_version,
        _sha256,
    )
else:
    from run_m4_acceptance import AcceptanceError, _atomic_write, _run
    from run_m5_external_canary import (
        _copy_file,
        _isolated_environment,
        _load_object,
        _runtime_version,
        _sha256,
    )

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "external" / "server-filesystem"
CHECKED_ARTIFACTS = ROOT / "artifacts" / "m5" / "filesystem"
DEFAULT_OUTPUT = Path("artifacts/m5/filesystem")
CHECK_OUTPUT = Path("artifacts/tmp/m5/filesystem")
PACKAGE = "@modelcontextprotocol/server-filesystem"
VERSION = "2026.7.10"
INTEGRITY = "sha512-Mmjg4anFBD5OzbPnGJOA0jPPN8645ERhQk38HQLpSenx1ox9bfdPkmAzUnNjeQtqQGFLtKe13J20RtLBmUKMZA=="
RESOLVED = (
    "https://registry.npmjs.org/@modelcontextprotocol/server-filesystem/-/"
    "server-filesystem-2026.7.10.tgz"
)
PROTOCOL_VERSION = "2025-11-25"
NODE_VERSION = "24.14.1"
RUNS = 10
TRACE_NAME = f"filesystem-{VERSION}-stdio.json"
CONTENT = "mcp-statecheck filesystem acceptance\n"
OUTSIDE_CONTENT = "outside sentinel\n"
TIMEOUT_SECONDS = 30


def _locked_target() -> tuple[str, str]:
    manifest = _load_object(BENCHMARK / "package.json", label="Filesystem manifest")
    lock = _load_object(BENCHMARK / "package-lock.json", label="Filesystem lock")
    if manifest.get("private") is not True:
        raise AcceptanceError("Filesystem benchmark package must be private")
    if manifest.get("engines") != {"node": NODE_VERSION}:
        raise AcceptanceError("Filesystem Node version is not exactly pinned")
    if manifest.get("dependencies") != {PACKAGE: VERSION}:
        raise AcceptanceError("Filesystem package version is not exactly pinned")
    if lock.get("lockfileVersion") != 3:
        raise AcceptanceError("Filesystem lock must use lockfile version 3")
    packages = lock.get("packages")
    entry = (
        packages.get(f"node_modules/{PACKAGE}")
        if isinstance(packages, Mapping)
        else None
    )
    if not isinstance(entry, Mapping) or (
        entry.get("version") != VERSION
        or entry.get("resolved") != RESOLVED
        or entry.get("integrity") != INTEGRITY
        or entry.get("bin") != {"mcp-server-filesystem": "dist/index.js"}
    ):
        raise AcceptanceError(
            "Filesystem target metadata does not match the pinned release"
        )
    return INTEGRITY, _sha256(BENCHMARK / "package-lock.json")


def _actions(sandbox: Path, outside: Path) -> tuple[Action, ...]:
    calls = (
        (
            "write-file",
            3,
            "write_file",
            {"path": str(sandbox / "state.txt"), "content": CONTENT},
        ),
        ("read-file", 4, "read_text_file", {"path": str(sandbox / "state.txt")}),
        (
            "write-outside",
            5,
            "write_file",
            {"path": str(outside / "sentinel.txt"), "content": "overwritten\n"},
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


def _response(result: ExecutionResult, action_id: str) -> Mapping[str, object]:
    for event in result.events:
        if (
            event.get("kind") == "response"
            and event.get("target_action_id") == action_id
        ):
            return event
    raise AcceptanceError(f"Filesystem trace is missing the {action_id} response")


def _payload(response: Mapping[str, object], action_id: str) -> Mapping[str, object]:
    if response.get("outcome") != "success" or not isinstance(
        response.get("payload"), Mapping
    ):
        raise AcceptanceError(f"Filesystem {action_id} did not return a result object")
    return response["payload"]  # type: ignore[return-value]


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "\n".join(_text(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return "\n".join(_text(item) for item in value)
    return ""


def _replace_paths(value: object, aliases: Mapping[Path, str]) -> object:
    if isinstance(value, str):
        spellings = []
        for path, alias in aliases.items():
            resolved = path.resolve()
            spellings.extend(
                (spelling, alias)
                for spelling in {
                    str(path),
                    path.as_posix(),
                    str(resolved),
                    resolved.as_posix(),
                }
            )
        for spelling, alias in sorted(spellings, key=lambda item: -len(item[0])):
            value = value.replace(spelling, alias)
        for alias in aliases.values():
            value = value.replace(f"{alias}\\", f"{alias}/")
        return value
    if isinstance(value, Mapping):
        return {key: _replace_paths(item, aliases) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_replace_paths(item, aliases) for item in value]
    return value


def _publish_trace(
    observed: Path,
    destination: Path,
    *,
    checked: Path,
    check: bool,
    label: str,
) -> bytes:
    payload = observed.read_bytes()
    if check:
        diagnostic = destination.with_name(f"observed-{destination.name}")
        observed_sha256 = hashlib.sha256(payload).hexdigest()
        try:
            expected = checked.read_bytes()
        except OSError as exc:
            _copy_file(observed, diagnostic)
            raise AcceptanceError(
                f"checked-in {label} trace is missing; "
                f"observed SHA-256 {observed_sha256}; "
                f"observed trace written to {diagnostic}"
            ) from exc
        if payload != expected:
            expected_sha256 = hashlib.sha256(expected).hexdigest()
            _copy_file(observed, diagnostic)
            raise AcceptanceError(
                f"{label} trace differs from checked-in evidence "
                f"(expected SHA-256 {expected_sha256}, "
                f"observed SHA-256 {observed_sha256}); "
                f"observed trace written to {diagnostic}"
            )
        diagnostic.unlink(missing_ok=True)
    _copy_file(observed, destination)
    return payload


def _canonical_work_root(path: str | Path) -> Path:
    return Path(path).resolve()


async def _probe_filesystem(
    *, node: Path, server: Path, sandbox: Path, outside: Path, trace: Path
) -> None:
    actions = _actions(sandbox, outside)
    result = await execute_stdio(
        actions, (str(node), str(server), str(sandbox)), timeout=10
    )
    if result.returncode != 0:
        raise AcceptanceError(
            f"Filesystem server exited with status {result.returncode}"
        )

    initialize = _payload(_response(result, "initialize"), "initialize")
    if initialize.get("protocolVersion") != PROTOCOL_VERSION:
        raise AcceptanceError(
            "Filesystem server negotiated an unexpected protocol version"
        )
    tools_payload = _payload(_response(result, "tools-list"), "tools-list")
    tools = tools_payload.get("tools")
    names = (
        {
            tool.get("name")
            for tool in tools
            if isinstance(tools, list) and isinstance(tool, Mapping)
        }
        if isinstance(tools, list)
        else set()
    )
    if not {"write_file", "read_text_file"}.issubset(names):
        raise AcceptanceError("Filesystem server is missing required tools")

    write_payload = _payload(_response(result, "write-file"), "write-file")
    read_payload = _payload(_response(result, "read-file"), "read-file")
    outside_payload = _payload(_response(result, "write-outside"), "write-outside")
    if write_payload.get("isError") is True or CONTENT not in _text(read_payload):
        raise AcceptanceError("Filesystem write/read state transition failed")
    if outside_payload.get("isError") is not True:
        raise AcceptanceError("Filesystem server accepted an outside write")
    if (sandbox / "state.txt").read_text(encoding="utf-8") != CONTENT:
        raise AcceptanceError("Filesystem sandbox content differs from the tool result")
    if {path.name for path in sandbox.iterdir()} != {"state.txt"}:
        raise AcceptanceError("Filesystem server created unexpected sandbox entries")
    if (outside / "sentinel.txt").read_text(encoding="utf-8") != OUTSIDE_CONTENT:
        raise AcceptanceError("Filesystem server changed the outside sentinel")

    aliases = {sandbox: "<sandbox>", outside: "<outside>"}
    recorder = TraceRecorder(
        protocol_version=PROTOCOL_VERSION,
        adapter="wire",
        sdk_version=f"{PACKAGE}@{VERSION}",
        transport="stdio",
        seed=0,
        fixture_id="filesystem-sandbox-state",
        cleanup={"server_reaped": True, "server_returncode": result.returncode},
        generation={
            "engine": "mcp-statecheck M5 acceptance",
            "outcome": "passed",
            "profile": "application-state",
        },
    )
    for action in actions:
        normalized = _replace_paths(action.to_dict(), aliases)
        assert isinstance(normalized, Mapping)
        recorder.record_action(normalized)
    for event in result.events:
        normalized = _replace_paths(event, aliases)
        assert isinstance(normalized, Mapping)
        recorder.record_event(normalized)
    recorder.write(trace)


def _probe_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--server", required=True, type=Path)
    parser.add_argument("--sandbox", required=True, type=Path)
    parser.add_argument("--outside", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        anyio.run(
            lambda: _probe_filesystem(
                node=args.node,
                server=args.server,
                sandbox=args.sandbox,
                outside=args.outside,
                trace=args.trace,
            )
        )
    except (AcceptanceError, OSError, ValueError) as exc:
        print(f"Filesystem probe failed: {exc}", file=sys.stderr)
        return 2
    return 0


def run(output: Path, *, check: bool) -> dict[str, object]:
    integrity, lock_sha256 = _locked_target()
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        raise AcceptanceError("Node.js and npm are required for Filesystem acceptance")

    with tempfile.TemporaryDirectory(
        prefix="mcp-statecheck-m5-filesystem-"
    ) as temporary:
        work = _canonical_work_root(temporary)
        environment = _isolated_environment(work)
        install = work / "install"
        install.mkdir()
        shutil.copy2(BENCHMARK / "package.json", install / "package.json")
        shutil.copy2(BENCHMARK / "package-lock.json", install / "package-lock.json")

        node_version = _runtime_version(
            [node, "--version"],
            cwd=work,
            environment=environment,
            label="Node version probe",
        )
        npm_version = _runtime_version(
            [npm, "--version"],
            cwd=work,
            environment=environment,
            label="npm version probe",
        )
        if node_version != NODE_VERSION:
            raise AcceptanceError(
                f"Filesystem acceptance requires Node {NODE_VERSION}, found {node_version}"
            )
        _run(
            [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=install,
            environment=environment,
            timeout=180,
            label="locked Filesystem install",
        )
        if _sha256(install / "package-lock.json") != lock_sha256:
            raise AcceptanceError("npm changed the copied Filesystem lock")
        server = (
            install
            / "node_modules"
            / "@modelcontextprotocol"
            / "server-filesystem"
            / "dist"
            / "index.js"
        )
        if not server.is_file():
            raise AcceptanceError("locked Filesystem package is missing dist/index.js")

        traces = work / "traces"
        traces.mkdir()
        trace_paths: list[Path] = []
        for attempt in range(1, RUNS + 1):
            case = work / "cases" / f"run-{attempt:02d}"
            sandbox = case / "allowed"
            outside = case / "outside"
            sandbox.mkdir(parents=True)
            outside.mkdir()
            (outside / "sentinel.txt").write_text(
                OUTSIDE_CONTENT, encoding="utf-8", newline="\n"
            )
            trace = traces / f"run-{attempt:02d}.json"
            _run(
                [
                    sys.executable,
                    Path(__file__).resolve(),
                    "_probe-filesystem",
                    "--node",
                    node,
                    "--server",
                    server,
                    "--sandbox",
                    sandbox,
                    "--outside",
                    outside,
                    "--trace",
                    trace,
                ],
                cwd=case,
                environment=environment,
                timeout=TIMEOUT_SECONDS,
                label=f"Filesystem acceptance run {attempt}/{RUNS}",
            )
            trace_paths.append(trace)

        first = trace_paths[0].read_bytes()
        if any(path.read_bytes() != first for path in trace_paths[1:]):
            raise AcceptanceError("Filesystem traces were not byte-identical")
        trace_sha256 = hashlib.sha256(first).hexdigest()
        _publish_trace(
            trace_paths[0],
            output / TRACE_NAME,
            checked=CHECKED_ARTIFACTS / TRACE_NAME,
            check=check,
            label="Filesystem",
        )
        summary: dict[str, object] = {
            "schema_version": 1,
            "milestone": "M5",
            "slice": "filesystem-application-server",
            "status": "passed",
            "protocol_version": PROTOCOL_VERSION,
            "transport": "stdio",
            "targets": {
                "filesystem": {
                    "package": {
                        "ecosystem": "npm",
                        "name": PACKAGE,
                        "version": VERSION,
                        "integrity": integrity,
                    },
                    "runtime": {"name": "node", "version": node_version},
                    "runs": {
                        "attempted": RUNS,
                        "passed": RUNS,
                        "byte_identical": True,
                    },
                    "state": {
                        "written_content_verified": RUNS,
                        "outside_write_rejected": RUNS,
                        "outside_sentinel_unchanged": RUNS,
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
                "node": node_version,
                "npm": npm_version,
                "python": platform.python_version(),
            },
            "isolation": {
                "ambient_credentials_removed": True,
                "allowed_directory": "fresh temporary directory per run",
                "os_sandbox": False,
            },
        }
        _atomic_write(output / "acceptance.json", summary)
        return summary


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_probe-filesystem":
        return _probe_main(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description="Run the pinned Filesystem server acceptance ten times."
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
        print(f"M5 Filesystem acceptance failed: {exc}", file=sys.stderr)
        return 2
    filesystem = summary["targets"]["filesystem"]  # type: ignore[index]
    print(
        "M5 Filesystem acceptance passed: "
        f"{filesystem['runs']['passed']}/{filesystem['runs']['attempted']} "  # type: ignore[index]
        f"byte-identical runs; {destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
