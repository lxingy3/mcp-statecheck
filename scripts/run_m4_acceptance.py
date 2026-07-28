"""Build and exercise clean mcp-statecheck installations."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

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
    "process_probe": 10,
}


class AcceptanceError(RuntimeError):
    """The clean-install acceptance check could not be completed."""


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)
    return environment


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                text=True,
                timeout=TIMEOUTS["process_probe"],
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
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
    peer_path: Path,
    package_version: str,
) -> int:
    artifact = _json_object(artifact_path, "JSON report")
    expected_artifact = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "adapter": "wire",
        "sdk_version": "none",
        "transport": "stdio",
        "fixture_id": "server-smoke",
    }
    if any(artifact.get(key) != value for key, value in expected_artifact.items()):
        raise AcceptanceError("JSON report metadata does not match the clean check")
    generation = artifact.get("generation")
    if not isinstance(generation, dict) or generation.get("outcome") != "passed":
        raise AcceptanceError("JSON report does not record a passing check")
    cleanup = artifact.get("cleanup")
    if cleanup != {"server_reaped": True, "server_returncode": 0}:
        raise AcceptanceError("JSON report does not prove stdio server cleanup")
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


def _stop_pid(pid: int) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                text=True,
                timeout=TIMEOUTS["process_probe"],
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


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
            versions[kind] = version

        reports = work / "reports"
        artifact = reports / "run.json"
        junit = reports / "run.xml"
        sarif = reports / "run.sarif"
        html = reports / "run.html"
        peer_report = reports / "peer.json"
        wheel_python, wheel_console = installs["wheel"]
        check = _run(
            [
                wheel_console,
                "check",
                "--stdio",
                "--timeout",
                "5",
                "--output",
                artifact,
                "--junit",
                junit,
                "--sarif",
                sarif,
                "--html",
                html,
                "--",
                wheel_python,
                (ROOT / "tests" / "fixtures" / "peer.py").resolve(),
                "--stdio",
                "--mode",
                "sdk-smoke",
                "--protocol-version",
                PROTOCOL_VERSION,
                "--report",
                peer_report,
            ],
            cwd=work,
            environment=environment,
            timeout=TIMEOUTS["check"],
            label="clean wheel stdio check",
            expected=(0, 1, 2),
        )
        pid = _validate_outputs(
            artifact,
            junit,
            sarif,
            html,
            peer_report,
            package_version,
        )
        if _pid_exists(pid, cwd=work, environment=environment):
            _stop_pid(pid)
            raise AcceptanceError(f"controlled peer process {pid} is still running")
        if check.returncode != 0:
            raise AcceptanceError(
                f"clean wheel stdio check exited with status {check.returncode}"
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
            "pythonpath_cleared": True,
            "source_tree_outside_cwd": True,
            "stdio": {
                "fixture": "sdk-smoke",
                "peer_clean_exit": True,
                "peer_reaped": True,
                "protocol_version": PROTOCOL_VERSION,
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
            "M4 acceptance passed: clean wheel and sdist installs, stdio check, "
            f"and four report formats; wrote {args.output}"
        )
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
