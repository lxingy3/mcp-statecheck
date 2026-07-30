"""Run the pinned M5 external MCP server canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from mcp_statecheck import __version__

if __package__:
    from .run_m4_acceptance import AcceptanceError, _atomic_write, _run
else:
    from run_m4_acceptance import AcceptanceError, _atomic_write, _run

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "external" / "server-everything"
CHECKED_ARTIFACTS = ROOT / "artifacts" / "m5"
DEFAULT_OUTPUT = Path("artifacts/m5")
PACKAGE = "@modelcontextprotocol/server-everything"
VERSION = "2026.7.4"
PROTOCOL_VERSION = "2025-11-25"
NODE_VERSION = "24.14.1"
RUNS = 10
TRACE_NAME = f"server-everything-{VERSION}-stdio.json"
TIMEOUT_SECONDS = 30


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} must be a JSON object")
    return value


def _locked_target() -> tuple[str, str]:
    manifest = _load_object(BENCHMARK / "package.json", label="canary package manifest")
    lock = _load_object(BENCHMARK / "package-lock.json", label="canary package lock")
    dependencies = manifest.get("dependencies")
    engines = manifest.get("engines")
    if dependencies != {PACKAGE: VERSION}:
        raise AcceptanceError("canary package manifest is not exactly pinned")
    if not isinstance(engines, Mapping) or engines.get("node") != NODE_VERSION:
        raise AcceptanceError("canary Node version is not exactly pinned")
    if lock.get("lockfileVersion") != 3:
        raise AcceptanceError("canary package lock must use lockfile version 3")
    packages = lock.get("packages")
    entry = (
        packages.get(f"node_modules/{PACKAGE}")
        if isinstance(packages, Mapping)
        else None
    )
    if not isinstance(entry, Mapping) or entry.get("version") != VERSION:
        raise AcceptanceError("canary package lock does not pin the target version")
    integrity = entry.get("integrity")
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise AcceptanceError("canary package lock is missing target integrity")
    return integrity, _sha256(BENCHMARK / "package-lock.json")


def _validate_trace(path: Path) -> None:
    trace = _load_object(path, label="canary trace")
    if (
        trace.get("schema_version") != 1
        or trace.get("protocol_version") != PROTOCOL_VERSION
        or trace.get("transport") != "stdio"
        or trace.get("generation")
        != {
            "engine": "mcp-statecheck CLI",
            "outcome": "passed",
            "profile": "quick",
        }
        or trace.get("cleanup")
        != {
            "server_reaped": True,
            "server_returncode": 0,
        }
        or "failure" in trace
    ):
        raise AcceptanceError("canary trace did not record a clean passing check")

    events = trace.get("normalized_events")
    if not isinstance(events, list):
        raise AcceptanceError("canary trace normalized events are missing")
    responses = {
        event.get("target_action_id"): event
        for event in events
        if isinstance(event, Mapping) and event.get("kind") == "response"
    }
    for action_id in ("initialize", "ping", "tools-list"):
        response = responses.get(action_id)
        if not isinstance(response, Mapping) or response.get("outcome") != "success":
            raise AcceptanceError(f"canary trace is missing {action_id} success")


def _runtime_version(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    label: str,
) -> str:
    completed = _run(
        command,
        cwd=cwd,
        environment=environment,
        timeout=TIMEOUT_SECONDS,
        label=label,
    )
    value = completed.stdout.strip()
    if not value:
        raise AcceptanceError(f"{label} did not report a version")
    return value.removeprefix("v")


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary_name)
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _isolated_environment(work: Path) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in (
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "SystemRoot",
            "WINDIR",
        )
        if name in os.environ
    }
    home = work / "home"
    temporary = work / "tmp"
    appdata = home / "appdata"
    local_appdata = home / "local-appdata"
    for directory in (home, temporary, appdata, local_appdata):
        directory.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "APPDATA": str(appdata),
            "HOME": str(home),
            "LOCALAPPDATA": str(local_appdata),
            "NO_COLOR": "1",
            "PYTHONUTF8": "1",
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
            "USERPROFILE": str(home),
            "npm_config_audit": "false",
            "npm_config_cache": str(work / "npm-cache"),
            "npm_config_fund": "false",
            "npm_config_ignore_scripts": "true",
            "npm_config_update_notifier": "false",
        }
    )
    return environment


def run(output: Path, *, check: bool) -> dict[str, object]:
    integrity, lock_sha256 = _locked_target()
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        raise AcceptanceError("Node.js and npm are required for the M5 canary")

    with tempfile.TemporaryDirectory(prefix="mcp-statecheck-m5-") as temporary:
        work = Path(temporary)
        environment = _isolated_environment(work)
        install = work / "server"
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
                f"M5 canary requires Node {NODE_VERSION}, found {node_version}"
            )

        _run(
            [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=install,
            environment=environment,
            timeout=180,
            label="locked canary install",
        )
        if _sha256(install / "package-lock.json") != lock_sha256:
            raise AcceptanceError("npm changed the copied canary package lock")

        server = (
            install
            / "node_modules"
            / "@modelcontextprotocol"
            / "server-everything"
            / "dist"
            / "index.js"
        )
        if not server.is_file():
            raise AcceptanceError("locked canary package is missing dist/index.js")

        traces = work / "traces"
        traces.mkdir()
        trace_paths: list[Path] = []
        for attempt in range(1, RUNS + 1):
            trace = traces / f"run-{attempt:02d}.json"
            _run(
                [
                    sys.executable,
                    "-m",
                    "mcp_statecheck.cli",
                    "check",
                    "--protocol-version",
                    PROTOCOL_VERSION,
                    "--timeout",
                    "10",
                    "--output",
                    trace,
                    "--stdio",
                    "--",
                    node,
                    server,
                    "stdio",
                ],
                cwd=work,
                environment=environment,
                timeout=TIMEOUT_SECONDS,
                label=f"external canary run {attempt}/{RUNS}",
            )
            _validate_trace(trace)
            trace_paths.append(trace)

        first = trace_paths[0].read_bytes()
        if any(path.read_bytes() != first for path in trace_paths[1:]):
            raise AcceptanceError("external canary traces were not byte-identical")
        trace_sha256 = hashlib.sha256(first).hexdigest()
        if check:
            checked = CHECKED_ARTIFACTS / TRACE_NAME
            try:
                expected = checked.read_bytes()
            except OSError as exc:
                raise AcceptanceError("checked-in M5 canary trace is missing") from exc
            if first != expected:
                raise AcceptanceError(
                    "external canary trace differs from checked-in evidence"
                )

        output_trace = output / TRACE_NAME
        _copy_file(trace_paths[0], output_trace)
        summary: dict[str, object] = {
            "schema_version": 1,
            "milestone": "M5",
            "slice": "pinned-external-server-canary",
            "status": "passed",
            "target": {
                "name": PACKAGE,
                "version": VERSION,
                "integrity": integrity,
            },
            "protocol_version": PROTOCOL_VERSION,
            "transport": "stdio",
            "runs": {
                "attempted": RUNS,
                "passed": RUNS,
                "byte_identical": True,
            },
            "cleanup": {
                "server_reaped": RUNS,
                "server_returncode_zero": RUNS,
            },
            "responses": {
                "initialize": RUNS,
                "ping": RUNS,
                "tools_list": RUNS,
            },
            "trace": {
                "file": TRACE_NAME,
                "sha256": trace_sha256,
                "sha256_counts": {trace_sha256: RUNS},
            },
            "package_lock_sha256": lock_sha256,
            "runtime": {
                "mcp_statecheck": __version__,
                "node": node_version,
                "npm": npm_version,
                "python": platform.python_version(),
            },
        }
        _atomic_write(output / "acceptance.json", summary)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the pinned external MCP server canary ten times."
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the generated trace with the checked-in M5 evidence",
    )
    args = parser.parse_args()

    try:
        if args.check and args.output is None:
            with tempfile.TemporaryDirectory(
                prefix="mcp-statecheck-m5-check-"
            ) as temporary:
                summary = run(Path(temporary), check=True)
            destination = "checked-in evidence"
        else:
            output = args.output or DEFAULT_OUTPUT
            summary = run(output, check=args.check)
            destination = str(output)
    except (AcceptanceError, OSError) as exc:
        print(f"M5 external canary failed: {exc}", file=sys.stderr)
        return 2
    print(
        "M5 external canary passed: "
        f"{summary['runs']['passed']}/{summary['runs']['attempted']} "
        f"byte-identical runs; {destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
