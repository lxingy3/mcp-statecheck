"""Build and exercise clean mcp-statecheck installations."""

from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path("artifacts/m4/acceptance.json")
PYTHON_VERSION = "3.12.13"
PROTOCOL_VERSION = "2025-11-25"
TIMEOUTS = {
    "build": 180,
    "venv": 120,
    "install": 180,
    "version": 15,
    "check": 30,
    "peer_ready": 10,
    "peer_stop": 10,
    "process_probe": 10,
}


class AcceptanceError(RuntimeError):
    """The clean-install acceptance check could not be completed."""


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)
    return environment


def _stop_process_tree(
    process: subprocess.Popen[str],
    *,
    extra_pids: Sequence[int] = (),
) -> None:
    if process.poll() is not None and not extra_pids:
        return
    if os.name == "nt":
        for pid in dict.fromkeys((process.pid, *extra_pids)):
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=TIMEOUTS["process_probe"],
                )
            except (OSError, subprocess.TimeoutExpired):
                result = None
            if result is None or result.returncode != 0:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            for pid in extra_pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    if process.poll() is None:
        try:
            process.wait(timeout=TIMEOUTS["process_probe"])
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=TIMEOUTS["process_probe"])


def _run(
    command: Sequence[str | Path],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: int,
    label: str,
    expected: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    argv = [str(part) for part in command]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _stop_process_tree(process)
        raise AcceptanceError(f"{label} exceeded its {timeout}-second timeout") from exc
    completed = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    if completed.returncode not in expected:
        detail = (completed.stderr or completed.stdout).strip()
        if len(detail) > 2000:
            detail = detail[-2000:]
        suffix = f": {detail}" if detail else ""
        raise AcceptanceError(
            f"{label} exited with status {completed.returncode}{suffix}"
        )
    return completed


def _one(directory: Path, pattern: str, label: str) -> Path:
    matches = tuple(directory.glob(pattern))
    if len(matches) != 1:
        raise AcceptanceError(f"expected one {label}, found {len(matches)}")
    return matches[0]


def _venv_executables(directory: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        return (
            directory / "Scripts" / "python.exe",
            directory / "Scripts" / "mcp-statecheck.exe",
        )
    return directory / "bin" / "python", directory / "bin" / "mcp-statecheck"


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} must contain a JSON object")
    return value


def _validate_outputs(
    artifact_path: Path,
    junit_path: Path,
    sarif_path: Path,
    html_path: Path,
    package_version: str,
    *,
    transport: str,
    cleanup: dict[str, object],
) -> None:
    artifact = _json_object(artifact_path, "JSON report")
    expected_artifact = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "adapter": "wire",
        "sdk_version": "none",
        "transport": transport,
        "fixture_id": "server-smoke",
    }
    if any(artifact.get(key) != value for key, value in expected_artifact.items()):
        raise AcceptanceError("JSON report metadata does not match the clean check")
    generation = artifact.get("generation")
    if not isinstance(generation, dict) or generation.get("outcome") != "passed":
        raise AcceptanceError("JSON report does not record a passing check")
    if artifact.get("cleanup") != cleanup:
        raise AcceptanceError(f"JSON report does not prove {transport} cleanup")
    if "failure" in artifact:
        raise AcceptanceError("passing JSON report contains a failure")
    response_targets = {
        event.get("target_action_id")
        for event in artifact.get("normalized_events", ())
        if isinstance(event, dict) and event.get("kind") == "response"
    }
    if response_targets != {"initialize", "ping", "tools-list"}:
        raise AcceptanceError("JSON report is missing smoke-check responses")

    try:
        suite = ET.parse(junit_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise AcceptanceError("JUnit report is not valid XML") from exc
    if (
        suite.tag != "testsuite"
        or suite.get("tests") != "1"
        or suite.get("errors") != "0"
        or suite.get("failures") != "0"
    ):
        raise AcceptanceError("JUnit report does not record one passing check")

    sarif = _json_object(sarif_path, "SARIF report")
    runs = sarif.get("runs")
    if sarif.get("version") != "2.1.0" or not isinstance(runs, list) or len(runs) != 1:
        raise AcceptanceError("SARIF report does not use the expected schema")
    run = runs[0]
    driver = run.get("tool", {}).get("driver", {}) if isinstance(run, dict) else {}
    if (
        not isinstance(driver, dict)
        or driver.get("name") != "mcp-statecheck"
        or driver.get("version") != package_version
        or run.get("results") != []
    ):
        raise AcceptanceError("SARIF report does not record a passing check")

    try:
        html = html_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AcceptanceError("HTML report could not be read") from exc
    if (
        not html.startswith("<!doctype html>")
        or "No failure detected" not in html
        or "Content-Security-Policy" not in html
        or "<script" in html.casefold()
    ):
        raise AcceptanceError("HTML report is not a script-free passing report")


def _validate_stdio_peer(peer_path: Path) -> int:
    peer = _json_object(peer_path, "controlled peer report")
    expected_peer = {
        "clean_exit": True,
        "initialize_protocol_versions": [PROTOCOL_VERSION],
        "methods": [
            "initialize",
            "notifications/initialized",
            "ping",
            "tools/list",
        ],
        "negotiated_protocol_version": PROTOCOL_VERSION,
    }
    if any(peer.get(key) != value for key, value in expected_peer.items()):
        raise AcceptanceError("controlled peer did not observe the smoke sequence")
    pid = peer.get("pid")
    if type(pid) is not int or pid <= 0:
        raise AcceptanceError("controlled peer report has an invalid PID")
    return pid


def _validate_http_peer(peer_path: Path, expected_pid: int) -> None:
    peer = _json_object(peer_path, "controlled HTTP peer report")
    expected_peer = {
        "accept_consistent": True,
        "authorization_consistent": True,
        "clean_exit": True,
        "delete_count": 1,
        "listener_closed": True,
        "methods": [
            "initialize",
            "notifications/initialized",
            "ping",
            "tools/list",
        ],
        "negotiated_protocol_version": PROTOCOL_VERSION,
        "protocol_version_preserved": True,
        "session_preserved": True,
        "pid": expected_pid,
    }
    if any(peer.get(key) != value for key, value in expected_peer.items()):
        raise AcceptanceError(
            "controlled HTTP peer did not prove headers, session, or cleanup"
        )


def _pid_exists(
    pid: int,
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> bool:
    if os.name == "nt":
        result = _run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            cwd=cwd,
            environment=environment,
            timeout=TIMEOUTS["process_probe"],
            label="Windows process probe",
        )
        return any(
            len(row) > 1 and row[1].strip().replace(",", "") == str(pid)
            for row in csv.reader(result.stdout.splitlines())
        )
    result = _run(
        ["ps", "-p", str(pid), "-o", "pid="],
        cwd=cwd,
        environment=environment,
        timeout=TIMEOUTS["process_probe"],
        label="POSIX process probe",
        expected=(0, 1),
    )
    return any(line.strip() == str(pid) for line in result.stdout.splitlines())


def _start_http_peer(
    command: Sequence[str | Path],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(part) for part in command],
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )


def _wait_for_http_peer(
    ready_path: Path,
    process: subprocess.Popen[str],
) -> tuple[str, str, int, int]:
    deadline = time.monotonic() + TIMEOUTS["peer_ready"]
    while time.monotonic() < deadline:
        if ready_path.is_file():
            ready = _json_object(ready_path, "controlled HTTP peer ready record")
            peer_pid = ready.get("pid")
            if type(peer_pid) is not int or peer_pid <= 0:
                raise AcceptanceError("controlled HTTP peer ready PID is invalid")
            url = ready.get("url")
            if not isinstance(url, str):
                raise AcceptanceError("controlled HTTP peer ready URL is invalid")
            parsed = urlsplit(url)
            try:
                port = parsed.port
            except ValueError as exc:
                raise AcceptanceError(
                    "controlled HTTP peer ready URL is invalid"
                ) from exc
            if (
                parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1"
                or port is None
                or parsed.path != "/mcp"
                or parsed.query
                or parsed.fragment
            ):
                raise AcceptanceError(
                    "controlled HTTP peer did not bind an explicit localhost endpoint"
                )
            return url, parsed.hostname, port, peer_pid
        returncode = process.poll()
        if returncode is not None:
            raise AcceptanceError(
                f"controlled HTTP peer exited before ready with status {returncode}"
            )
        time.sleep(0.05)
    raise AcceptanceError("controlled HTTP peer did not become ready")


def _finish_http_peer(
    process: subprocess.Popen[str],
    *,
    peer_pid: int | None,
    cwd: Path,
    environment: Mapping[str, str],
) -> tuple[str, str]:
    try:
        stdout, stderr = process.communicate(
            input="",
            timeout=TIMEOUTS["peer_stop"],
        )
    except subprocess.TimeoutExpired as exc:
        _stop_process_tree(
            process,
            extra_pids=(peer_pid,) if peer_pid is not None else (),
        )
        if peer_pid is not None and _pid_exists(
            peer_pid,
            cwd=cwd,
            environment=environment,
        ):
            raise AcceptanceError(
                f"controlled HTTP peer {peer_pid} survived timeout cleanup"
            ) from exc
        raise AcceptanceError(
            "controlled HTTP peer did not stop after stdin EOF"
        ) from exc
    if process.returncode != 0:
        detail = (stderr or stdout).strip()
        suffix = f": {detail[-2000:]}" if detail else ""
        raise AcceptanceError(
            f"controlled HTTP peer exited with status {process.returncode}{suffix}"
        )
    return stdout, stderr


def _listener_accepts(host: str, port: int) -> bool:
    try:
        connection = socket.create_connection((host, port), timeout=0.5)
    except OSError:
        return False
    connection.close()
    return True


def _assert_secret_absent(
    secret: str,
    paths: Sequence[Path],
    outputs: Sequence[str],
) -> None:
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AcceptanceError(f"could not inspect {path.name} for secrets") from exc
        if secret in content:
            raise AcceptanceError(f"HTTP secret leaked into {path.name}")
    if any(secret in output for output in outputs):
        raise AcceptanceError("HTTP secret leaked into process output")


def _atomic_write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _accept() -> dict[str, object]:
    uv = shutil.which("uv")
    if uv is None:
        raise AcceptanceError("uv is required")
    with (ROOT / "pyproject.toml").open("rb") as handle:
        package_version = tomllib.load(handle)["project"]["version"]
    if not isinstance(package_version, str) or not package_version:
        raise AcceptanceError("project version is invalid")
    expected_version = f"mcp-statecheck {package_version}"
    environment = _clean_environment()

    with tempfile.TemporaryDirectory(prefix="mcp-statecheck-m4-") as temporary:
        work = Path(temporary).resolve()
        if work.is_relative_to(ROOT.resolve()):
            raise AcceptanceError(
                "clean-install directory must be outside the source tree"
            )
        distributions = work / "dist"
        distributions.mkdir()
        for kind in ("wheel", "sdist"):
            _run(
                [uv, "build", f"--{kind}", "--out-dir", distributions],
                cwd=ROOT,
                environment=environment,
                timeout=TIMEOUTS["build"],
                label=f"{kind} build",
            )
        wheel = _one(distributions, "mcp_statecheck-*.whl", "wheel")
        sdist = _one(distributions, "mcp_statecheck-*.tar.gz", "sdist")

        installs: dict[str, tuple[Path, Path]] = {}
        import_origins: dict[str, str] = {}
        versions: dict[str, str] = {}
        for kind, distribution in (("wheel", wheel), ("sdist", sdist)):
            venv = work / f"{kind}-venv"
            _run(
                [uv, "venv", "--python", PYTHON_VERSION, venv],
                cwd=work,
                environment=environment,
                timeout=TIMEOUTS["venv"],
                label=f"{kind} virtual environment creation",
            )
            python, console = _venv_executables(venv)
            _run(
                [uv, "pip", "install", "--strict", "--python", python, distribution],
                cwd=work,
                environment=environment,
                timeout=TIMEOUTS["install"],
                label=f"{kind} installation",
            )
            if not python.is_file() or not console.is_file():
                raise AcceptanceError(f"{kind} did not install its console script")
            runtime = _run(
                [python, "-c", "import platform; print(platform.python_version())"],
                cwd=work,
                environment=environment,
                timeout=TIMEOUTS["version"],
                label=f"{kind} Python version check",
            ).stdout.strip()
            if runtime != PYTHON_VERSION:
                raise AcceptanceError(
                    f"{kind} used Python {runtime}, expected {PYTHON_VERSION}"
                )
            origin_text = _run(
                [
                    python,
                    "-c",
                    "import mcp_statecheck; print(mcp_statecheck.__file__)",
                ],
                cwd=work,
                environment=environment,
                timeout=TIMEOUTS["version"],
                label=f"{kind} import origin check",
            ).stdout.strip()
            origin = Path(origin_text).resolve()
            if (
                not origin.is_file()
                or not origin.is_relative_to(venv.resolve())
                or origin.is_relative_to(ROOT.resolve())
            ):
                raise AcceptanceError(
                    f"{kind} imported mcp-statecheck outside its clean environment"
                )
            version = _run(
                [console, "--version"],
                cwd=work,
                environment=environment,
                timeout=TIMEOUTS["version"],
                label=f"{kind} console version check",
            ).stdout.strip()
            if version != expected_version:
                raise AcceptanceError(
                    f"{kind} console reported {version!r}, expected {expected_version!r}"
                )
            installs[kind] = (python, console)
            import_origins[kind] = "isolated"
            versions[kind] = version

        reports = work / "reports"
        stdio_artifact = reports / "stdio.json"
        stdio_junit = reports / "stdio.xml"
        stdio_sarif = reports / "stdio.sarif"
        stdio_html = reports / "stdio.html"
        stdio_peer_report = reports / "stdio-peer.json"
        peer_fixture = (ROOT / "tests" / "fixtures" / "peer.py").resolve()
        wheel_python, wheel_console = installs["wheel"]
        stdio_check = _run(
            [
                wheel_console,
                "check",
                "--stdio",
                "--timeout",
                "5",
                "--output",
                stdio_artifact,
                "--junit",
                stdio_junit,
                "--sarif",
                stdio_sarif,
                "--html",
                stdio_html,
                "--",
                wheel_python,
                peer_fixture,
                "--stdio",
                "--mode",
                "sdk-smoke",
                "--protocol-version",
                PROTOCOL_VERSION,
                "--report",
                stdio_peer_report,
            ],
            cwd=work,
            environment=environment,
            timeout=TIMEOUTS["check"],
            label="clean wheel stdio check",
            expected=(0, 1, 2),
        )
        _validate_outputs(
            stdio_artifact,
            stdio_junit,
            stdio_sarif,
            stdio_html,
            package_version,
            transport="stdio",
            cleanup={"server_reaped": True, "server_returncode": 0},
        )
        stdio_pid = _validate_stdio_peer(stdio_peer_report)
        if _pid_exists(stdio_pid, cwd=work, environment=environment):
            raise AcceptanceError(
                f"controlled stdio peer process {stdio_pid} is still running"
            )
        if stdio_check.returncode != 0:
            raise AcceptanceError(
                f"clean wheel stdio check exited with status {stdio_check.returncode}"
            )

        secret_variable = "MCP_STATECHECK_M4_TOKEN"
        http_secret = secrets.token_urlsafe(32)
        http_environment = {**environment, secret_variable: http_secret}
        http_artifact = reports / "http.json"
        http_junit = reports / "http.xml"
        http_sarif = reports / "http.sarif"
        http_html = reports / "http.html"
        http_ready = reports / "http-ready.json"
        http_peer_report = reports / "http-peer.json"
        http_peer = _start_http_peer(
            [
                wheel_python,
                peer_fixture,
                "--http",
                "--mode",
                "sdk-smoke",
                "--protocol-version",
                PROTOCOL_VERSION,
                "--ready",
                http_ready,
                "--report",
                http_peer_report,
                "--authorization-env",
                secret_variable,
            ],
            cwd=work,
            environment=http_environment,
        )
        http_peer_pid: int | None = None
        try:
            http_url, http_host, http_port, http_peer_pid = _wait_for_http_peer(
                http_ready,
                http_peer,
            )
            http_check = _run(
                [
                    wheel_console,
                    "check",
                    "--url",
                    http_url,
                    "--header-env",
                    f"Authorization={secret_variable}",
                    "--timeout",
                    "5",
                    "--output",
                    http_artifact,
                    "--junit",
                    http_junit,
                    "--sarif",
                    http_sarif,
                    "--html",
                    http_html,
                ],
                cwd=work,
                environment=http_environment,
                timeout=TIMEOUTS["check"],
                label="clean wheel Streamable HTTP check",
                expected=(0, 1, 2),
            )
        finally:
            http_peer_stdout, http_peer_stderr = _finish_http_peer(
                http_peer,
                peer_pid=http_peer_pid,
                cwd=work,
                environment=http_environment,
            )

        assert http_peer_pid is not None
        _validate_outputs(
            http_artifact,
            http_junit,
            http_sarif,
            http_html,
            package_version,
            transport="streamable-http",
            cleanup={"client_closed": True},
        )
        _validate_http_peer(http_peer_report, http_peer_pid)
        if _pid_exists(http_peer_pid, cwd=work, environment=http_environment):
            raise AcceptanceError(
                f"controlled HTTP peer process {http_peer_pid} is still running"
            )
        if _listener_accepts(http_host, http_port):
            raise AcceptanceError("controlled HTTP peer listener is still accepting")
        _assert_secret_absent(
            http_secret,
            [
                http_artifact,
                http_junit,
                http_sarif,
                http_html,
                http_ready,
                http_peer_report,
            ],
            [
                http_check.stdout,
                http_check.stderr,
                http_peer_stdout,
                http_peer_stderr,
            ],
        )
        if http_check.returncode != 0:
            raise AcceptanceError(
                "clean wheel Streamable HTTP check exited with status "
                f"{http_check.returncode}"
            )

        return {
            "schema_version": 1,
            "milestone": "M4",
            "status": "passed",
            "package_version": package_version,
            "python": PYTHON_VERSION,
            "builds": {
                "sdist": sdist.name,
                "wheel": wheel.name,
            },
            "clean_installs": ["sdist", "wheel"],
            "console_versions": versions,
            "import_origins": import_origins,
            "pythonpath_cleared": True,
            "source_tree_outside_cwd": True,
            "stdio": {
                "fixture": "sdk-smoke",
                "peer_clean_exit": True,
                "peer_reaped": True,
                "protocol_version": PROTOCOL_VERSION,
                "status": "passed",
            },
            "streamable_http": {
                "authorization_secret_absent": True,
                "fixture": "sdk-smoke",
                "listener_closed": True,
                "peer_clean_exit": True,
                "peer_reaped": True,
                "protocol_version": PROTOCOL_VERSION,
                "session_deleted": True,
                "status": "passed",
            },
            "reports": ["html", "json", "junit", "sarif"],
            "timeouts_seconds": TIMEOUTS,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        summary = _accept()
    except (AcceptanceError, OSError, KeyError, TypeError, ValueError) as exc:
        summary = {
            "schema_version": 1,
            "milestone": "M4",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        returncode = 2
    else:
        returncode = 0
    try:
        _atomic_write(args.output, summary)
    except OSError as exc:
        print(f"M4 acceptance could not write {args.output}: {exc}", file=sys.stderr)
        return 2
    if returncode:
        print(summary["error"], file=sys.stderr)
    else:
        print(
            "M4 acceptance passed: clean wheel and sdist installs, real stdio "
            "and Streamable HTTP checks, and four report formats per transport; "
            f"wrote {args.output}"
        )
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
