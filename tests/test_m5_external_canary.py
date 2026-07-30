import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts import run_m5_external_canary

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "@modelcontextprotocol/server-everything"
VERSION = "2026.7.4"
TRACE_NAME = f"server-everything-{VERSION}-stdio.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_m5_external_canary_is_pinned_and_reproducible() -> None:
    benchmark = ROOT / "benchmarks" / "external" / "server-everything"
    manifest_path = benchmark / "package.json"
    lock_path = benchmark / "package-lock.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))

    assert manifest["private"] is True
    assert manifest["engines"]["node"] == "24.14.1"
    assert manifest["dependencies"] == {PACKAGE: VERSION}
    assert lock["lockfileVersion"] == 3
    locked_package = lock["packages"][f"node_modules/{PACKAGE}"]
    assert locked_package["version"] == VERSION
    assert locked_package["integrity"].startswith("sha512-")

    artifacts = ROOT / "artifacts" / "m5"
    trace_path = artifacts / TRACE_NAME
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    acceptance = json.loads((artifacts / "acceptance.json").read_text(encoding="utf-8"))

    assert trace["schema_version"] == 1
    assert trace["protocol_version"] == "2025-11-25"
    assert trace["transport"] == "stdio"
    assert trace["generation"] == {
        "engine": "mcp-statecheck CLI",
        "outcome": "passed",
        "profile": "quick",
    }
    assert trace["cleanup"] == {
        "server_reaped": True,
        "server_returncode": 0,
    }
    assert "failure" not in trace

    assert acceptance["schema_version"] == 1
    assert acceptance["milestone"] == "M5"
    assert acceptance["slice"] == "pinned-external-server-canary"
    assert acceptance["status"] == "passed"
    assert acceptance["target"] == {
        "integrity": locked_package["integrity"],
        "name": PACKAGE,
        "version": VERSION,
    }
    assert acceptance["protocol_version"] == "2025-11-25"
    assert acceptance["transport"] == "stdio"
    assert acceptance["runs"] == {
        "attempted": 10,
        "byte_identical": True,
        "passed": 10,
    }
    assert acceptance["trace"] == {
        "file": TRACE_NAME,
        "sha256": _sha256(trace_path),
        "sha256_counts": {_sha256(trace_path): 10},
    }
    assert acceptance["package_lock_sha256"] == _sha256(lock_path)


def test_m5_canary_does_not_expose_host_secrets_to_the_external_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("NPM_TOKEN", "npm-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")

    environment = run_m5_external_canary._isolated_environment(tmp_path)

    assert "GITHUB_TOKEN" not in environment
    assert "NPM_TOKEN" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["USERPROFILE"] == str(tmp_path / "home")
    assert environment["TEMP"] == str(tmp_path / "tmp")
    assert environment["TMP"] == str(tmp_path / "tmp")
    assert environment["npm_config_cache"] == str(tmp_path / "npm-cache")


def test_m5_check_without_output_does_not_rewrite_checked_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(output: Path, *, check: bool) -> dict[str, object]:
        captured.update(output=output, check=check)
        return {"runs": {"attempted": 10, "passed": 10}}

    monkeypatch.setattr(run_m5_external_canary, "run", run)
    monkeypatch.setattr(sys, "argv", ["run_m5_external_canary.py", "--check"])

    assert run_m5_external_canary.main() == 0
    assert captured["check"] is True
    assert captured["output"] != run_m5_external_canary.DEFAULT_OUTPUT
    assert not Path(captured["output"]).exists()
