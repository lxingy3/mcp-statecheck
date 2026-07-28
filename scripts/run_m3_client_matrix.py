"""Run the real Python/TypeScript SDK client transport matrix."""

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
DEFAULT_OUTPUT = ROOT / "artifacts" / "m3"
REQUESTED_PROTOCOL_VERSION = "2025-11-25"
NODE_VERSION = "24.14.1"
RUNNER_IDS = ("python-v1", "python-v2", "typescript-v1", "typescript-v2")
PROTOCOL_VERSIONS = ("2025-06-18", "2025-11-25")
TRANSPORTS = ("stdio", "streamable-http")
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


def _controlled_http_peer(mode: str, protocol_version: str):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tests.fixtures.peer import ControlledHTTPPeer

    return ControlledHTTPPeer(mode, protocol_version=protocol_version)


def _load_runners() -> dict[str, dict[str, object]]:
    with CONFIG.open("rb") as handle:
        config = tomllib.load(handle)
    if config.get("schema_version") != 1:
        raise ValueError("unsupported benchmark schema")
    if tuple(config.get("protocol_versions", ())) != PROTOCOL_VERSIONS:
        raise ValueError("benchmark protocol matrix does not match M3")
    if tuple(config.get("transports", ())) != TRANSPORTS:
        raise ValueError("benchmark transport matrix does not match M3")
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


def _adapter_command(runner_id: str) -> tuple[list[str], dict[str, str]]:
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
        return [str(python), "-m", "mcp_statecheck.adapters.python_client"], environment

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
    transport: str,
    target: list[str] | str,
    *,
    mode: str = "sdk-smoke",
) -> Envelope:
    return Envelope(
        command_id=f"{runner_id}:{transport}:{protocol_version}:{mode}",
        kind="run",
        payload={
            "actions": [action.to_dict() for action in ACTIONS],
            "runner_id": runner_id,
            "sdk_version": runner["version"],
            "target": target,
            "transport": transport,
        },
    )


def _peer_observation(
    protocol_version: str,
    transport: str,
) -> dict[str, object]:
    observation: dict[str, object] = {
        "initialize_protocol_version": REQUESTED_PROTOCOL_VERSION,
        "kind": "peer_observation",
        "method_order": list(PEER_METHODS),
        "negotiated_protocol_version": protocol_version,
    }
    if transport == "streamable-http":
        observation.update(
            {
                "post_accept_headers_valid": True,
                "protocol_headers_preserved": True,
                "session_deleted": True,
                "session_preserved": True,
            }
        )
    return observation


def _expected_events(
    protocol_version: str,
    transport: str,
) -> list[dict[str, object]]:
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
        _peer_observation(protocol_version, transport),
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


async def _exchange(
    request: Envelope,
    runner_id: str,
    *,
    timeout: float = 120,
) -> tuple[Envelope, StdioTransport]:
    command, environment = _adapter_command(runner_id)
    transport = StdioTransport(
        command,
        cwd=ROOT,
        env=environment,
        timeout=timeout,
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
    return response, transport


async def _stdio_peer_result(
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


def _http_peer_observation(
    peer: object,
    protocol_version: str,
) -> dict[str, object]:
    state = peer.state  # type: ignore[attr-defined]
    if tuple(state.post_methods) != PEER_METHODS:
        raise RuntimeError("controlled HTTP peer observed the wrong method order")
    if tuple(state.stdio_methods) != PEER_METHODS:
        raise RuntimeError("controlled HTTP peer dispatched the wrong method order")
    if state.initialize_protocol_versions != [REQUESTED_PROTOCOL_VERSION]:
        raise RuntimeError("HTTP SDK client requested an unexpected protocol version")
    if state.post_session_ids != [None, *([state.session_id] * 4)]:
        raise RuntimeError("HTTP SDK client did not preserve its session")
    if state.post_protocol_versions != [None, *([protocol_version] * 4)]:
        raise RuntimeError("HTTP SDK client did not preserve its protocol version")
    if any(
        accept is None
        or "application/json" not in accept.lower()
        or "text/event-stream" not in accept.lower()
        for accept in state.post_accepts
    ):
        raise RuntimeError("HTTP SDK client sent an invalid Accept header")
    if (
        state.delete_count != 1
        or state.delete_session_ids != [state.session_id]
        or state.delete_protocol_versions != [protocol_version]
    ):
        raise RuntimeError("HTTP SDK client did not delete its session")
    if any(session_id != state.session_id for session_id in state.session_ids) or any(
        version != protocol_version for version in state.protocol_versions
    ):
        raise RuntimeError("HTTP SDK client lost headers on its optional GET stream")
    return _peer_observation(protocol_version, "streamable-http")


async def _run_stdio_cell(
    runner_id: str,
    runner: Mapping[str, object],
    protocol_version: str,
    report: Path,
) -> dict[str, object]:
    request = _request(
        runner_id,
        runner,
        protocol_version,
        "stdio",
        _peer_command(protocol_version, report),
    )
    response, transport = await _exchange(request, runner_id)
    events, sdk_version, runtime_version, client_closed = _result_payload(
        response,
        command_id=request.command_id,
        runner_id=runner_id,
        runner=runner,
    )
    peer = await _stdio_peer_result(report, protocol_version)
    events.append(_peer_observation(protocol_version, "stdio"))
    if events != _expected_events(protocol_version, "stdio"):
        raise RuntimeError(f"{runner_id} returned an unexpected stdio trace")
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


async def _run_http_cell(
    runner_id: str,
    runner: Mapping[str, object],
    protocol_version: str,
) -> dict[str, object]:
    with _controlled_http_peer("sdk-smoke", protocol_version) as peer:
        request = _request(
            runner_id,
            runner,
            protocol_version,
            "streamable-http",
            peer.url,
        )
        response, transport = await _exchange(request, runner_id)
        observation = _http_peer_observation(peer, protocol_version)

    events, sdk_version, runtime_version, client_closed = _result_payload(
        response,
        command_id=request.command_id,
        runner_id=runner_id,
        runner=runner,
    )
    events.append(observation)
    if events != _expected_events(protocol_version, "streamable-http"):
        raise RuntimeError(f"{runner_id} returned an unexpected HTTP trace")
    return {
        "cleanup": {
            "adapter_reaped": transport.returncode is not None,
            "adapter_returncode": transport.returncode,
            "client_closed": client_closed,
            "listener_closed": True,
            "session_deleted": True,
        },
        "events": events,
        "runtime_version": runtime_version,
        "sdk_version": sdk_version,
    }


async def _expect_adapter_timeout(request: Envelope, runner_id: str) -> None:
    command, environment = _adapter_command(runner_id)
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


async def _run_stdio_cleanup_probe(
    runner_id: str,
    runner: Mapping[str, object],
    report: Path,
) -> None:
    request = _request(
        runner_id,
        runner,
        REQUESTED_PROTOCOL_VERSION,
        "stdio",
        _peer_command(
            REQUESTED_PROTOCOL_VERSION,
            report,
            mode="sdk-hang",
        ),
        mode="sdk-hang",
    )
    await _expect_adapter_timeout(request, runner_id)
    await _stdio_peer_result(
        report,
        REQUESTED_PROTOCOL_VERSION,
        require_clean_exit=False,
    )


async def _run_http_cleanup_probe(
    runner_id: str,
    runner: Mapping[str, object],
) -> None:
    with _controlled_http_peer("sdk-hang", REQUESTED_PROTOCOL_VERSION) as peer:
        request = _request(
            runner_id,
            runner,
            REQUESTED_PROTOCOL_VERSION,
            "streamable-http",
            peer.url,
            mode="sdk-hang",
        )
        await _expect_adapter_timeout(request, runner_id)
        if tuple(peer.state.post_methods) != PEER_METHODS:
            raise RuntimeError("HTTP cleanup probe did not reach the hanging call")


async def _build(output: Path) -> list[Path]:
    runners = _load_runners()
    _prepare_python(runners)
    _prepare_typescript(runners)
    results: dict[tuple[str, str, str], dict[str, object]] = {}
    with TemporaryDirectory(prefix="mcp-statecheck-m3-peer-") as temporary:
        reports = Path(temporary)
        for protocol_version in PROTOCOL_VERSIONS:
            for runner_id in RUNNER_IDS:
                results[("stdio", runner_id, protocol_version)] = await _run_stdio_cell(
                    runner_id,
                    runners[runner_id],
                    protocol_version,
                    reports / f"{runner_id}-{protocol_version}.json",
                )
                results[
                    ("streamable-http", runner_id, protocol_version)
                ] = await _run_http_cell(
                    runner_id,
                    runners[runner_id],
                    protocol_version,
                )
        for runner_id in ("python-v1", "typescript-v1"):
            await _run_stdio_cleanup_probe(
                runner_id,
                runners[runner_id],
                reports / f"{runner_id}-cleanup.json",
            )
            await _run_http_cleanup_probe(runner_id, runners[runner_id])

    written: list[Path] = []
    for transport in TRANSPORTS:
        for protocol_version in PROTOCOL_VERSIONS:
            for runner_id in RUNNER_IDS:
                result = results[(transport, runner_id, protocol_version)]
                recorder = TraceRecorder(
                    protocol_version=protocol_version,
                    adapter=runner_id,
                    sdk_version=str(result["sdk_version"]),
                    transport=transport,
                    seed=0,
                    fixture_id="sdk-client-smoke",
                    cleanup=result["cleanup"],  # type: ignore[arg-type]
                    generation={
                        "engine": "real SDK client transport matrix",
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
                    recorder.write(
                        output / transport / f"{runner_id}-{protocol_version}.json"
                    )
                )
    return written


def _check(expected: Path) -> None:
    with TemporaryDirectory(prefix="mcp-statecheck-m3-check-") as temporary:
        actual = Path(temporary)
        generated = anyio.run(_build, actual)
        expected_names = {path.relative_to(actual) for path in generated}
        checked_names = {
            path.relative_to(expected) for path in expected.rglob("*.json")
        }
        if checked_names != expected_names:
            raise RuntimeError("checked-in M3 artifact set does not match the matrix")
        for path in generated:
            checked = expected / path.relative_to(actual)
            if path.read_bytes() != checked.read_bytes():
                raise RuntimeError(
                    f"checked-in M3 artifact is stale: {path.relative_to(actual)}"
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.check:
        _check(args.output)
        print("M3 client matrix passed: 16/16 real SDK transport cells match artifacts")
    else:
        written = anyio.run(_build, args.output)
        print(f"M3 client matrix passed: wrote {len(written)} real SDK traces")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
