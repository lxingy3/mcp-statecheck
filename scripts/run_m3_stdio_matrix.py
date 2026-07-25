"""Run the real Python/TypeScript SDK client matrix over stdio."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio

from mcp_statecheck.adapters.jsonl import Envelope
from mcp_statecheck.model import Action, ActionKind
from mcp_statecheck.trace import TraceRecorder
from mcp_statecheck.transports.stdio import StdioTimeout, StdioTransport

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "benchmarks" / "mcp-v2.toml"
PEER = ROOT / "tests" / "fixtures" / "peer.py"
TYPESCRIPT_RUNNER = (
    ROOT / "src" / "mcp_statecheck" / "adapters" / "typescript_client.mts"
)
PYTHON_ENVIRONMENTS = {
    runner_id: ROOT
    / "src"
    / "mcp_statecheck"
    / "adapters"
    / "python"
    / runner_id.removeprefix("python-")
    for runner_id in ("python-v1", "python-v2")
}
TYPESCRIPT_ENVIRONMENTS = {
    runner_id: ROOT
    / "src"
    / "mcp_statecheck"
    / "adapters"
    / "typescript"
    / runner_id.removeprefix("typescript-")
    for runner_id in ("typescript-v1", "typescript-v2")
}
DEFAULT_OUTPUT = ROOT / "artifacts" / "m3" / "stdio"
REQUESTED_PROTOCOL_VERSION = "2025-11-25"
NODE_VERSION = "24.14.1"
RUNNER_IDS = ("python-v1", "python-v2", "typescript-v1", "typescript-v2")
PROTOCOL_VERSIONS = ("2025-06-18", "2025-11-25")
PEER_METHODS = (
    "initialize",
    "notifications/initialized",
    "ping",
    "tools/list",
    "tools/call",
)
ACTIONS = (
    Action("connect", ActionKind.CONNECT),
    Action(
        "initialize",
        ActionKind.INITIALIZE,
        protocol_version=REQUESTED_PROTOCOL_VERSION,
        capabilities={},
    ),
    Action("initialized", ActionKind.INITIALIZED),
    Action("ping", ActionKind.REQUEST, method="ping", payload={}),
    Action("list-tools", ActionKind.REQUEST, method="tools/list", payload={}),
    Action(
        "call-echo",
        ActionKind.REQUEST,
        method="tools/call",
        payload={"name": "echo", "arguments": {"text": "hello"}},
    ),
    Action("close", ActionKind.CLOSE),
)


def _load_runners() -> dict[str, dict[str, object]]:
    with CONFIG.open("rb") as handle:
        config = tomllib.load(handle)
    if config.get("schema_version") != 1:
        raise ValueError("unsupported benchmark schema")
    if tuple(config.get("protocol_versions", ())) != PROTOCOL_VERSIONS:
        raise ValueError("benchmark protocol matrix does not match M3")
    raw_runners = config.get("runners")
    if not isinstance(raw_runners, list) or not all(
        isinstance(runner, dict) for runner in raw_runners
    ):
        raise TypeError("benchmark runners must be an array of tables")
    runners = {runner.get("id"): runner for runner in raw_runners}
    if set(runners) != set(RUNNER_IDS):
        raise ValueError("benchmark runner IDs do not match M3")
    return runners  # type: ignore[return-value]


def _prepare_typescript(runners: Mapping[str, Mapping[str, object]]) -> None:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        raise RuntimeError("Node.js and npm are required for the TypeScript runners")
    installed = subprocess.run(
        [node, "--version"],
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if installed != f"v{NODE_VERSION}":
        raise RuntimeError(f"Node.js {NODE_VERSION} is required; found {installed}")

    for runner_id, environment in TYPESCRIPT_ENVIRONMENTS.items():
        manifest = json.loads(
            (environment / "package.json").read_text(encoding="utf-8")
        )
        runner = runners[runner_id]
        package = runner["package"]
        version = runner["version"]
        if manifest["dependencies"].get(package) != version:
            raise ValueError(f"{runner_id} package.json does not match benchmark pins")
        completed = subprocess.run(
            [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=180,
        )
        if completed.returncode:
            raise RuntimeError(
                f"{runner_id} npm ci failed:\n{completed.stdout}{completed.stderr}"
            )


def _prepare_python(runners: Mapping[str, Mapping[str, object]]) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required for the Python runners")
    for runner_id, environment in PYTHON_ENVIRONMENTS.items():
        with (environment / "pyproject.toml").open("rb") as handle:
            manifest = tomllib.load(handle)
        runner = runners[runner_id]
        dependencies = [
            f"{runner['package']}=={runner['version']}",
            *runner.get("dependencies", []),  # type: ignore[arg-type]
        ]
        if manifest["project"]["dependencies"] != dependencies:
            raise ValueError(
                f"{runner_id} pyproject.toml does not match benchmark pins"
            )
        completed = subprocess.run(
            [
                uv,
                "sync",
                "--project",
                str(environment),
                "--locked",
                "--python",
                "3.12.13",
                "--no-dev",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=180,
        )
        if completed.returncode:
            raise RuntimeError(
                f"{runner_id} uv sync failed:\n{completed.stdout}{completed.stderr}"
            )


def _adapter_command(
    runner_id: str,
    runner: Mapping[str, object],
) -> tuple[list[str], dict[str, str]]:
    environment = os.environ.copy()
    source = str(ROOT / "src")
    environment["PYTHONPATH"] = (
        source
        if not environment.get("PYTHONPATH")
        else source + os.pathsep + environment["PYTHONPATH"]
    )
    if runner_id.startswith("python-"):
        python = (
            PYTHON_ENVIRONMENTS[runner_id]
            / ".venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        if not python.is_file():
            raise RuntimeError(f"{runner_id} environment is not synchronized")
        return [
            str(python),
            "-m",
            "mcp_statecheck.adapters.python_client",
        ], environment

    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required for the TypeScript runners")
    environment["MCP_STATECHECK_NODE_ENV"] = str(TYPESCRIPT_ENVIRONMENTS[runner_id])
    return [node, str(TYPESCRIPT_RUNNER)], environment


def _peer_command(
    protocol_version: str,
    report: Path,
    *,
    mode: str = "sdk-smoke",
) -> list[str]:
    return [
        sys.executable,
        str(PEER),
        "--stdio",
        "--mode",
        mode,
        "--protocol-version",
        protocol_version,
        "--report",
        str(report),
    ]


def _request(
    runner_id: str,
    runner: Mapping[str, object],
    protocol_version: str,
    report: Path,
    *,
    mode: str = "sdk-smoke",
) -> Envelope:
    return Envelope(
        command_id=f"{runner_id}:{protocol_version}:{mode}",
        kind="run",
        payload={
            "actions": [action.to_dict() for action in ACTIONS],
            "peer_command": _peer_command(
                protocol_version,
                report,
                mode=mode,
            ),
            "runner_id": runner_id,
            "sdk_version": runner["version"],
        },
    )


def _expected_events(protocol_version: str) -> list[dict[str, object]]:
    return [
        {
            "kind": "response",
            "method": "initialize",
            "protocol_version": protocol_version,
            "server_info": {"name": "controlled-peer", "version": "0.1"},
            "target_action_id": "initialize",
        },
        {
            "kind": "response",
            "method": "ping",
            "target_action_id": "ping",
        },
        {
            "kind": "response",
            "method": "tools/list",
            "target_action_id": "list-tools",
            "tool_names": ["echo"],
        },
        {
            "is_error": False,
            "kind": "response",
            "method": "tools/call",
            "target_action_id": "call-echo",
            "text": "hello",
        },
        {
            "initialize_protocol_version": REQUESTED_PROTOCOL_VERSION,
            "kind": "peer_observation",
            "method_order": list(PEER_METHODS),
            "negotiated_protocol_version": protocol_version,
        },
    ]


def _process_running(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        )
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        )
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        try:
            return bool(
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                and exit_code.value == 259
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _read_peer_report(report: Path) -> dict[str, object]:
    result: object = None
    with anyio.fail_after(10):
        while not isinstance(result, dict):
            try:
                result = json.loads(report.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                await anyio.sleep(0.05)
        pid = result.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise RuntimeError("controlled SDK peer report has an invalid PID")
        while _process_running(pid):
            await anyio.sleep(0.05)
    final = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(final, dict):
        raise RuntimeError("controlled SDK peer report must be an object")
    return final


def _result_payload(
    response: Envelope,
    *,
    command_id: str,
    runner_id: str,
    runner: Mapping[str, object],
) -> tuple[list[dict[str, object]], str, str, bool]:
    if response.command_id != command_id or response.kind != "result":
        raise ValueError(f"{runner_id} returned the wrong envelope identity")
    if set(response.payload) != {
        "cleanup",
        "events",
        "runner_id",
        "runtime_version",
        "sdk_version",
    }:
        raise ValueError(f"{runner_id} result fields do not match schema v1")
    if response.payload["runner_id"] != runner_id:
        raise ValueError(f"{runner_id} returned a different runner ID")
    if response.payload["sdk_version"] != runner["version"]:
        raise ValueError(f"{runner_id} loaded an unexpected SDK version")

    runtime_version = response.payload["runtime_version"]
    raw_events = response.payload["events"]
    cleanup = response.payload["cleanup"]
    if not isinstance(runtime_version, str) or not runtime_version:
        raise TypeError(f"{runner_id} returned an invalid runtime version")
    if not isinstance(raw_events, list) or not all(
        isinstance(event, dict) for event in raw_events
    ):
        raise TypeError(f"{runner_id} returned invalid normalized events")
    if not isinstance(cleanup, dict) or cleanup.get("client_closed") is not True:
        raise RuntimeError(f"{runner_id} did not close its SDK client")
    return (
        raw_events,  # type: ignore[return-value]
        str(response.payload["sdk_version"]),
        runtime_version,
        True,
    )


async def _peer_result(
    report: Path,
    protocol_version: str,
    *,
    require_clean_exit: bool = True,
) -> dict[str, object]:
    result = await _read_peer_report(report)
    if require_clean_exit and result.get("clean_exit") is not True:
        raise RuntimeError("controlled SDK peer did not exit cleanly")
    if tuple(result.get("methods", ())) != PEER_METHODS:
        raise RuntimeError("controlled SDK peer observed the wrong method order")
    if result.get("initialize_protocol_versions") != [REQUESTED_PROTOCOL_VERSION]:
        raise RuntimeError("SDK client requested an unexpected protocol version")
    if result.get("negotiated_protocol_version") != protocol_version:
        raise RuntimeError("controlled SDK peer negotiated the wrong protocol version")
    return result


async def _run_cell(
    runner_id: str,
    runner: Mapping[str, object],
    protocol_version: str,
    report: Path,
) -> dict[str, object]:
    request = _request(
        runner_id,
        runner,
        protocol_version,
        report,
    )
    command, environment = _adapter_command(runner_id, runner)
    transport = StdioTransport(
        command,
        cwd=ROOT,
        env=environment,
        timeout=120,
        shutdown_timeout=5,
    )
    try:
        async with transport:
            await transport.send(request.to_dict())
            response = Envelope.from_dict(await transport.receive())
    except Exception as exc:
        raise RuntimeError(
            f"{runner_id} adapter exchange failed:\n{transport.stderr}"
        ) from exc
    if transport.returncode != 0:
        raise RuntimeError(
            f"{runner_id} adapter exited {transport.returncode}:\n{transport.stderr}"
        )

    events, sdk_version, runtime_version, client_closed = _result_payload(
        response,
        command_id=request.command_id,
        runner_id=runner_id,
        runner=runner,
    )
    peer = await _peer_result(report, protocol_version)
    events.append(
        {
            "initialize_protocol_version": REQUESTED_PROTOCOL_VERSION,
            "kind": "peer_observation",
            "method_order": list(PEER_METHODS),
            "negotiated_protocol_version": protocol_version,
        }
    )
    if events != _expected_events(protocol_version):
        raise RuntimeError(f"{runner_id} returned an unexpected normalized trace")
    return {
        "cleanup": {
            "adapter_reaped": transport.returncode is not None,
            "adapter_returncode": transport.returncode,
            "client_closed": client_closed,
            "peer_clean_exit": peer["clean_exit"],
            "peer_reaped": True,
        },
        "events": events,
        "runtime_version": runtime_version,
        "sdk_version": sdk_version,
    }


async def _run_cleanup_probe(
    runner_id: str,
    runner: Mapping[str, object],
    report: Path,
) -> None:
    request = _request(
        runner_id,
        runner,
        REQUESTED_PROTOCOL_VERSION,
        report,
        mode="sdk-hang",
    )
    command, environment = _adapter_command(runner_id, runner)
    transport = StdioTransport(
        command,
        cwd=ROOT,
        env=environment,
        timeout=5,
        shutdown_timeout=5,
    )
    try:
        async with transport:
            await transport.send(request.to_dict())
            await transport.receive()
    except StdioTimeout:
        pass
    else:
        raise RuntimeError(f"{runner_id} cleanup probe did not reach its hard timeout")
    if transport.returncode in {None, 0}:
        raise RuntimeError(f"{runner_id} cleanup probe did not reap its adapter")
    await _peer_result(
        report,
        REQUESTED_PROTOCOL_VERSION,
        require_clean_exit=False,
    )


async def _build(output: Path) -> list[Path]:
    runners = _load_runners()
    _prepare_python(runners)
    _prepare_typescript(runners)
    results: dict[tuple[str, str], dict[str, object]] = {}
    with TemporaryDirectory(prefix="mcp-statecheck-m3-peer-") as temporary:
        reports = Path(temporary)
        for protocol_version in PROTOCOL_VERSIONS:
            for runner_id in RUNNER_IDS:
                results[(runner_id, protocol_version)] = await _run_cell(
                    runner_id,
                    runners[runner_id],
                    protocol_version,
                    reports / f"{runner_id}-{protocol_version}.json",
                )
        for runner_id in ("python-v1", "typescript-v1"):
            await _run_cleanup_probe(
                runner_id,
                runners[runner_id],
                reports / f"{runner_id}-cleanup.json",
            )

    written: list[Path] = []
    for protocol_version in PROTOCOL_VERSIONS:
        for runner_id in RUNNER_IDS:
            result = results[(runner_id, protocol_version)]
            recorder = TraceRecorder(
                protocol_version=protocol_version,
                adapter=runner_id,
                sdk_version=str(result["sdk_version"]),
                transport="stdio",
                seed=0,
                fixture_id="sdk-client-smoke",
                cleanup=result["cleanup"],  # type: ignore[arg-type]
                generation={
                    "engine": "real SDK stdio matrix",
                    "requested_protocol_version": REQUESTED_PROTOCOL_VERSION,
                    "runner_id": runner_id,
                    "runtime_version": result["runtime_version"],
                },
            )
            for action in ACTIONS:
                recorder.record_action(action.to_dict())
            for event in result["events"]:  # type: ignore[union-attr]
                recorder.record_event(event)
            written.append(
                recorder.write(output / f"{runner_id}-{protocol_version}.json")
            )
    return written


def _check(expected: Path) -> None:
    with TemporaryDirectory(prefix="mcp-statecheck-m3-check-") as temporary:
        actual = Path(temporary)
        generated = anyio.run(_build, actual)
        expected_names = {path.name for path in generated}
        checked_names = {path.name for path in expected.glob("*.json")}
        if checked_names != expected_names:
            raise RuntimeError("checked-in M3 artifact set does not match the matrix")
        for path in generated:
            checked = expected / path.name
            if path.read_bytes() != checked.read_bytes():
                raise RuntimeError(f"checked-in M3 artifact is stale: {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.check:
        _check(args.output)
        print("M3 stdio matrix passed: 8/8 real SDK client cells match artifacts")
    else:
        written = anyio.run(_build, args.output)
        print(f"M3 stdio matrix passed: wrote {len(written)} real SDK client traces")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
