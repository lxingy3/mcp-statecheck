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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio

from ._controlled_peer import ControlledHTTPPeer
from .adapters.jsonl import Envelope
from .model import Action, ActionKind
from .trace import TraceRecorder
from .transports.stdio import StdioTimeout, StdioTransport

PACKAGE_ROOT = Path(__file__).resolve().parent
ASSET_ROOT = PACKAGE_ROOT / "adapters"
DEFAULT_OUTPUT = Path("artifacts/m3")
REQUESTED_PROTOCOL_VERSION = "2025-11-25"
NODE_VERSION = "24.14.1"
RUNNER_IDS = ("python-v1", "python-v2", "typescript-v1", "typescript-v2")
PROTOCOL_VERSIONS = ("2025-06-18", "2025-11-25")
TRANSPORTS = ("stdio", "streamable-http")
_ISOLATION_VARIABLES = {
    "MCP_STATECHECK_NODE_ENV",
    "NODE_PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "UV_PROJECT_ENVIRONMENT",
    "VIRTUAL_ENV",
}
_PYTHON_ADAPTER_FILES = (
    Path("__init__.py"),
    Path("model.py"),
    Path("adapters/__init__.py"),
    Path("adapters/jsonl.py"),
    Path("adapters/python_client.py"),
)
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


class MatrixError(RuntimeError):
    """Base class for expected matrix command failures."""


class MatrixFailure(MatrixError):
    """One SDK cell or checked artifact did not match the matrix oracle."""


class MatrixInfrastructureError(MatrixError):
    """The matrix could not run because its environment or config was invalid."""


@dataclass(frozen=True)
class _MatrixRuntime:
    workdir: Path
    import_root: Path
    python_environments: dict[str, Path]
    typescript_environments: dict[str, Path]
    typescript_runner: Path


def _default_config() -> Path:
    bundled = PACKAGE_ROOT / "benchmarks" / "mcp-v2.toml"
    if bundled.is_file():
        return bundled
    source = PACKAGE_ROOT.parents[1] / "benchmarks" / "mcp-v2.toml"
    if source.is_file():
        return source
    raise MatrixInfrastructureError("bundled benchmark config is missing")


def _materialize_runtime(workdir: Path) -> _MatrixRuntime:
    import_root = workdir / "adapter"
    for relative in _PYTHON_ADAPTER_FILES:
        target = import_root / "mcp_statecheck" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PACKAGE_ROOT / relative, target)

    python_environments: dict[str, Path] = {}
    typescript_environments: dict[str, Path] = {}
    for runner_id in ("python-v1", "python-v2"):
        source = ASSET_ROOT / "python" / runner_id.removeprefix("python-")
        target = workdir / "python" / runner_id.removeprefix("python-")
        target.mkdir(parents=True)
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copy2(source / name, target / name)
        python_environments[runner_id] = target
    for runner_id in ("typescript-v1", "typescript-v2"):
        source = ASSET_ROOT / "typescript" / runner_id.removeprefix("typescript-")
        target = workdir / "typescript" / runner_id.removeprefix("typescript-")
        target.mkdir(parents=True)
        for name in ("package.json", "package-lock.json"):
            shutil.copy2(source / name, target / name)
        typescript_environments[runner_id] = target
    typescript_runner = workdir / "typescript_client.mts"
    shutil.copy2(ASSET_ROOT / "typescript_client.mts", typescript_runner)
    return _MatrixRuntime(
        workdir=workdir,
        import_root=import_root,
        python_environments=python_environments,
        typescript_environments=typescript_environments,
        typescript_runner=typescript_runner,
    )


def _isolated_environment() -> dict[str, str]:
    blocked = {name.casefold() for name in _ISOLATION_VARIABLES}
    return {
        name: value
        for name, value in os.environ.items()
        if name.casefold() not in blocked
    }


def _controlled_http_peer(mode: str, protocol_version: str):
    return ControlledHTTPPeer(mode, protocol_version=protocol_version)


def _load_runners(config_path: Path) -> dict[str, dict[str, object]]:
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    if config.get("schema_version") != 1:
        raise MatrixInfrastructureError("unsupported benchmark schema")
    if tuple(config.get("protocol_versions", ())) != PROTOCOL_VERSIONS:
        raise MatrixInfrastructureError("benchmark protocol matrix does not match M3")
    if tuple(config.get("transports", ())) != TRANSPORTS:
        raise MatrixInfrastructureError("benchmark transport matrix does not match M3")
    raw_runners = config.get("runners")
    if not isinstance(raw_runners, list) or not all(
        isinstance(runner, dict) for runner in raw_runners
    ):
        raise MatrixInfrastructureError("benchmark runners must be an array of tables")
    runners = {runner.get("id"): runner for runner in raw_runners}
    if set(runners) != set(RUNNER_IDS):
        raise MatrixInfrastructureError("benchmark runner IDs do not match M3")
    return runners  # type: ignore[return-value]


def _prepare_typescript(
    runners: Mapping[str, Mapping[str, object]],
    runtime: _MatrixRuntime,
) -> None:
    environment_variables = _isolated_environment()
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        raise MatrixInfrastructureError(
            "Node.js and npm are required for the TypeScript runners"
        )
    installed = subprocess.run(
        [node, "--version"],
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if installed != f"v{NODE_VERSION}":
        raise MatrixInfrastructureError(
            f"Node.js {NODE_VERSION} is required; found {installed}"
        )

    for runner_id, environment in runtime.typescript_environments.items():
        manifest = json.loads(
            (environment / "package.json").read_text(encoding="utf-8")
        )
        runner = runners[runner_id]
        package = runner["package"]
        version = runner["version"]
        if manifest["dependencies"].get(package) != version:
            raise MatrixInfrastructureError(
                f"{runner_id} package.json does not match benchmark pins"
            )
        completed = subprocess.run(
            [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=environment,
            capture_output=True,
            check=False,
            env=environment_variables,
            text=True,
            timeout=180,
        )
        if completed.returncode:
            raise MatrixInfrastructureError(
                f"{runner_id} npm ci failed:\n{completed.stdout}{completed.stderr}"
            )


def _prepare_python(
    runners: Mapping[str, Mapping[str, object]],
    runtime: _MatrixRuntime,
) -> None:
    environment_variables = _isolated_environment()
    uv = shutil.which("uv")
    if uv is None:
        raise MatrixInfrastructureError("uv is required for the Python runners")
    for runner_id, environment in runtime.python_environments.items():
        with (environment / "pyproject.toml").open("rb") as handle:
            manifest = tomllib.load(handle)
        runner = runners[runner_id]
        dependencies = [
            f"{runner['package']}=={runner['version']}",
            *runner.get("dependencies", []),  # type: ignore[arg-type]
        ]
        if manifest["project"]["dependencies"] != dependencies:
            raise MatrixInfrastructureError(
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
            env=environment_variables,
            text=True,
            timeout=180,
        )
        if completed.returncode:
            raise MatrixInfrastructureError(
                f"{runner_id} uv sync failed:\n{completed.stdout}{completed.stderr}"
            )


def _adapter_command(
    runner_id: str,
    runtime: _MatrixRuntime,
) -> tuple[list[str], dict[str, str]]:
    environment = _isolated_environment()
    environment["PYTHONPATH"] = str(runtime.import_root)
    if runner_id.startswith("python-"):
        python = (
            runtime.python_environments[runner_id]
            / ".venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        if not python.is_file():
            raise MatrixInfrastructureError(
                f"{runner_id} environment is not synchronized"
            )
        return [str(python), "-m", "mcp_statecheck.adapters.python_client"], environment

    node = shutil.which("node")
    if node is None:
        raise MatrixInfrastructureError(
            "Node.js is required for the TypeScript runners"
        )
    environment["MCP_STATECHECK_NODE_ENV"] = str(
        runtime.typescript_environments[runner_id]
    )
    return [node, str(runtime.typescript_runner)], environment


def _peer_command(
    protocol_version: str,
    report: Path,
    *,
    mode: str = "sdk-smoke",
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "mcp_statecheck._controlled_peer",
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
            raise MatrixInfrastructureError(
                "controlled SDK peer report has an invalid PID"
            )
        while _process_running(pid):
            await anyio.sleep(0.05)
    final = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(final, dict):
        raise MatrixInfrastructureError("controlled SDK peer report must be an object")
    return final


def _result_payload(
    response: Envelope,
    *,
    command_id: str,
    runner_id: str,
    runner: Mapping[str, object],
) -> tuple[list[dict[str, object]], str, str, bool]:
    if response.command_id != command_id:
        raise MatrixInfrastructureError(
            f"{runner_id} returned the wrong envelope identity"
        )
    if response.kind == "failure":
        if set(response.payload) != {"error_type", "message", "runner_id"}:
            raise MatrixInfrastructureError(
                f"{runner_id} failure fields do not match schema v1"
            )
        error_type = response.payload["error_type"]
        message = response.payload["message"]
        if (
            response.payload["runner_id"] != runner_id
            or not isinstance(error_type, str)
            or not error_type
            or not isinstance(message, str)
            or not message
        ):
            raise MatrixInfrastructureError(
                f"{runner_id} returned an invalid SDK failure"
            )
        raise MatrixFailure(f"{runner_id} SDK cell failed ({error_type}): {message}")
    if response.kind != "result":
        raise MatrixInfrastructureError(
            f"{runner_id} returned an unsupported envelope kind"
        )
    if set(response.payload) != {
        "cleanup",
        "events",
        "runner_id",
        "runtime_version",
        "sdk_version",
    }:
        raise MatrixInfrastructureError(
            f"{runner_id} result fields do not match schema v1"
        )
    if response.payload["runner_id"] != runner_id:
        raise MatrixInfrastructureError(f"{runner_id} returned a different runner ID")
    if response.payload["sdk_version"] != runner["version"]:
        raise MatrixInfrastructureError(f"{runner_id} loaded an unexpected SDK version")

    runtime_version = response.payload["runtime_version"]
    raw_events = response.payload["events"]
    cleanup = response.payload["cleanup"]
    if not isinstance(runtime_version, str) or not runtime_version:
        raise MatrixInfrastructureError(
            f"{runner_id} returned an invalid runtime version"
        )
    if not isinstance(raw_events, list) or not all(
        isinstance(event, dict) for event in raw_events
    ):
        raise MatrixInfrastructureError(
            f"{runner_id} returned invalid normalized events"
        )
    if not isinstance(cleanup, dict) or cleanup.get("client_closed") is not True:
        raise MatrixFailure(f"{runner_id} did not close its SDK client")
    return (
        raw_events,  # type: ignore[return-value]
        str(response.payload["sdk_version"]),
        runtime_version,
        True,
    )


async def _exchange(
    request: Envelope,
    runner_id: str,
    runtime: _MatrixRuntime,
    *,
    timeout: float = 120,
) -> tuple[Envelope, StdioTransport]:
    command, environment = _adapter_command(runner_id, runtime)
    transport = StdioTransport(
        command,
        cwd=runtime.workdir,
        env=environment,
        timeout=timeout,
        shutdown_timeout=5,
    )
    try:
        await transport.start()
    except Exception as exc:
        raise MatrixInfrastructureError(
            f"{runner_id} adapter could not start:\n{transport.stderr}"
        ) from exc
    try:
        await transport.send(request.to_dict())
        response = Envelope.from_dict(await transport.receive())
    except StdioTimeout as exc:
        raise MatrixFailure(
            f"{runner_id} SDK cell exceeded its hard timeout:\n{transport.stderr}"
        ) from exc
    except Exception as exc:
        raise MatrixInfrastructureError(
            f"{runner_id} adapter exchange failed:\n{transport.stderr}"
        ) from exc
    finally:
        try:
            await transport.close()
        except Exception as exc:
            raise MatrixFailure(
                f"{runner_id} adapter could not be reaped:\n{transport.stderr}"
            ) from exc
    if transport.returncode != 0:
        raise MatrixInfrastructureError(
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
        raise MatrixFailure("controlled SDK peer did not exit cleanly")
    if tuple(result.get("methods", ())) != PEER_METHODS:
        raise MatrixFailure("controlled SDK peer observed the wrong method order")
    if result.get("initialize_protocol_versions") != [REQUESTED_PROTOCOL_VERSION]:
        raise MatrixFailure("SDK client requested an unexpected protocol version")
    if result.get("negotiated_protocol_version") != protocol_version:
        raise MatrixFailure("controlled SDK peer negotiated the wrong protocol version")
    return result


def _http_peer_observation(
    peer: object,
    protocol_version: str,
) -> dict[str, object]:
    state = peer.state  # type: ignore[attr-defined]
    if tuple(state.post_methods) != PEER_METHODS:
        raise MatrixFailure("controlled HTTP peer observed the wrong method order")
    if tuple(state.stdio_methods) != PEER_METHODS:
        raise MatrixFailure("controlled HTTP peer dispatched the wrong method order")
    if state.initialize_protocol_versions != [REQUESTED_PROTOCOL_VERSION]:
        raise MatrixFailure("HTTP SDK client requested an unexpected protocol version")
    if state.post_session_ids != [None, *([state.session_id] * 4)]:
        raise MatrixFailure("HTTP SDK client did not preserve its session")
    if state.post_protocol_versions != [None, *([protocol_version] * 4)]:
        raise MatrixFailure("HTTP SDK client did not preserve its protocol version")
    if any(
        accept is None
        or "application/json" not in accept.lower()
        or "text/event-stream" not in accept.lower()
        for accept in state.post_accepts
    ):
        raise MatrixFailure("HTTP SDK client sent an invalid Accept header")
    if (
        state.delete_count != 1
        or state.delete_session_ids != [state.session_id]
        or state.delete_protocol_versions != [protocol_version]
    ):
        raise MatrixFailure("HTTP SDK client did not delete its session")
    if any(session_id != state.session_id for session_id in state.session_ids) or any(
        version != protocol_version for version in state.protocol_versions
    ):
        raise MatrixFailure("HTTP SDK client lost headers on its optional GET stream")
    return _peer_observation(protocol_version, "streamable-http")


async def _run_stdio_cell(
    runner_id: str,
    runner: Mapping[str, object],
    protocol_version: str,
    report: Path,
    runtime: _MatrixRuntime,
) -> dict[str, object]:
    request = _request(
        runner_id,
        runner,
        protocol_version,
        "stdio",
        _peer_command(protocol_version, report),
    )
    response, transport = await _exchange(request, runner_id, runtime)
    events, sdk_version, runtime_version, client_closed = _result_payload(
        response,
        command_id=request.command_id,
        runner_id=runner_id,
        runner=runner,
    )
    peer = await _stdio_peer_result(report, protocol_version)
    events.append(_peer_observation(protocol_version, "stdio"))
    if events != _expected_events(protocol_version, "stdio"):
        raise MatrixFailure(f"{runner_id} returned an unexpected stdio trace")
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
    runtime: _MatrixRuntime,
) -> dict[str, object]:
    with _controlled_http_peer("sdk-smoke", protocol_version) as peer:
        request = _request(
            runner_id,
            runner,
            protocol_version,
            "streamable-http",
            peer.url,
        )
        response, transport = await _exchange(request, runner_id, runtime)
        observation = _http_peer_observation(peer, protocol_version)

    events, sdk_version, runtime_version, client_closed = _result_payload(
        response,
        command_id=request.command_id,
        runner_id=runner_id,
        runner=runner,
    )
    events.append(observation)
    if events != _expected_events(protocol_version, "streamable-http"):
        raise MatrixFailure(f"{runner_id} returned an unexpected HTTP trace")
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


async def _expect_adapter_timeout(
    request: Envelope,
    runner_id: str,
    runtime: _MatrixRuntime,
) -> None:
    command, environment = _adapter_command(runner_id, runtime)
    transport = StdioTransport(
        command,
        cwd=runtime.workdir,
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
        raise MatrixFailure(f"{runner_id} cleanup probe did not reach its hard timeout")
    if transport.returncode in {None, 0}:
        raise MatrixFailure(f"{runner_id} cleanup probe did not reap its adapter")


async def _run_stdio_cleanup_probe(
    runner_id: str,
    runner: Mapping[str, object],
    report: Path,
    runtime: _MatrixRuntime,
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
    await _expect_adapter_timeout(request, runner_id, runtime)
    await _stdio_peer_result(
        report,
        REQUESTED_PROTOCOL_VERSION,
        require_clean_exit=False,
    )


async def _run_http_cleanup_probe(
    runner_id: str,
    runner: Mapping[str, object],
    runtime: _MatrixRuntime,
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
        await _expect_adapter_timeout(request, runner_id, runtime)
        if tuple(peer.state.post_methods) != PEER_METHODS:
            raise MatrixFailure("HTTP cleanup probe did not reach the hanging call")


async def _build(config_path: Path, output: Path) -> list[Path]:
    results: dict[tuple[str, str, str], dict[str, object]] = {}
    with TemporaryDirectory(prefix="mcp-statecheck-matrix-") as temporary:
        workdir = Path(temporary)
        runtime = _materialize_runtime(workdir / "runners")
        runners = _load_runners(config_path)
        _prepare_python(runners, runtime)
        _prepare_typescript(runners, runtime)
        reports = workdir / "peer-reports"
        for protocol_version in PROTOCOL_VERSIONS:
            for runner_id in RUNNER_IDS:
                results[("stdio", runner_id, protocol_version)] = await _run_stdio_cell(
                    runner_id,
                    runners[runner_id],
                    protocol_version,
                    reports / f"{runner_id}-{protocol_version}.json",
                    runtime,
                )
                results[
                    ("streamable-http", runner_id, protocol_version)
                ] = await _run_http_cell(
                    runner_id,
                    runners[runner_id],
                    protocol_version,
                    runtime,
                )
        for runner_id in ("python-v1", "typescript-v1"):
            await _run_stdio_cleanup_probe(
                runner_id,
                runners[runner_id],
                reports / f"{runner_id}-cleanup.json",
                runtime,
            )
            await _run_http_cleanup_probe(runner_id, runners[runner_id], runtime)

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


def _run_build(config_path: Path | None, output: Path) -> list[Path]:
    config = _default_config() if config_path is None else config_path
    try:
        return anyio.run(_build, config, output)
    except MatrixError:
        raise
    except Exception as exc:
        raise MatrixInfrastructureError(f"{type(exc).__name__}: {exc}") from exc


def run_matrix(config_path: Path | None, output: Path) -> list[Path]:
    """Run all 16 locked SDK transport cells and write their traces."""

    return _run_build(config_path, output)


def check_matrix(config_path: Path | None, expected: Path) -> None:
    """Regenerate all cells and compare them with an explicit golden directory."""

    if not expected.is_dir():
        raise MatrixInfrastructureError(
            f"expected artifact directory does not exist: {expected}"
        )
    with TemporaryDirectory(prefix="mcp-statecheck-m3-check-") as temporary:
        actual = Path(temporary)
        generated = _run_build(config_path, actual)
        try:
            expected_names = {path.relative_to(actual) for path in generated}
            checked_names = {
                path.relative_to(expected) for path in expected.rglob("*.json")
            }
        except OSError as exc:
            raise MatrixInfrastructureError(
                f"expected artifacts could not be inspected: {exc}"
            ) from exc
        if checked_names != expected_names:
            raise MatrixFailure("checked-in M3 artifact set does not match the matrix")
        for path in generated:
            checked = expected / path.relative_to(actual)
            try:
                matches = path.read_bytes() == checked.read_bytes()
            except OSError as exc:
                raise MatrixInfrastructureError(
                    f"matrix artifacts could not be compared: {exc}"
                ) from exc
            if not matches:
                raise MatrixFailure(
                    f"checked-in M3 artifact is stale: {path.relative_to(actual)}"
                )


def script_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", nargs="?", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        if args.check:
            check_matrix(args.config, args.output)
        else:
            written = run_matrix(args.config, args.output)
    except MatrixFailure as exc:
        print(f"mcp-statecheck matrix: {exc}", file=sys.stderr)
        return 1
    except MatrixError as exc:
        print(f"mcp-statecheck matrix: {exc}", file=sys.stderr)
        return 2
    if args.check:
        print("M3 client matrix passed: 16/16 real SDK transport cells match artifacts")
    else:
        print(f"M3 client matrix passed: wrote {len(written)} real SDK traces")
    return 0


if __name__ == "__main__":
    raise SystemExit(script_main())
