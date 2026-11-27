"""Capture and compare tool catalogs from two released Playwright MCP builds."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

EXAMPLE = Path(__file__).resolve().parent
PROTOCOL = "2025-11-25"
CLI = Path(sys.executable).with_name(
    "mcp-statecheck.exe" if os.name == "nt" else "mcp-statecheck"
)
# Keep unrelated runner credentials out of the checker and the server.
RUNTIME_ENV = {
    key: value
    for key, value in os.environ.items()
    if key.upper()
    in {"HOME", "PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERPROFILE", "WINDIR"}
}


def capture(package: str, report: Path, workdir: Path) -> None:
    cli = EXAMPLE / "node_modules" / package / "cli.js"
    subprocess.run(
        [
            str(CLI),
            "check",
            "--protocol-version",
            PROTOCOL,
            "--timeout",
            "10",
            "--output",
            str(report),
            "--stdio",
            "--",
            "node",
            str(cli),
            "--isolated",
        ],
        check=True,
        cwd=workdir,
        env=RUNTIME_ENV,
        timeout=60,
    )


def compare(
    baseline: Path,
    candidate: Path,
    output: Path,
    expected_changes: dict[str, list[str]],
) -> None:
    expected_code = 1 if expected_changes else 0
    expected_status = "changed" if expected_changes else "match"
    result = subprocess.run(
        [str(CLI), "compare", str(baseline), str(candidate)],
        capture_output=True,
        encoding="utf-8",
        env=RUNTIME_ENV,
        timeout=30,
    )
    if result.stdout:
        output.write_text(result.stdout, encoding="utf-8")
    if result.returncode != expected_code:
        raise SystemExit(
            f"{output.stem}: expected exit {expected_code}, got {result.returncode}\n"
            f"{result.stdout}{result.stderr}"
        )

    expected = {
        "added": [],
        "changed": expected_changes,
        "protocol_version": PROTOCOL,
        "removed": [],
        "schema_version": 1,
        "status": expected_status,
        "tools_capability_changed": False,
        "transport": "stdio",
    }
    if json.loads(result.stdout) != expected:
        raise SystemExit(
            f"{output.stem}: unexpected comparison\n"
            f"Expected:\n{json.dumps(expected, indent=2)}\nActual:\n{result.stdout}"
        )
    print(f"{output.stem}: {expected_status} (exit {expected_code})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=EXAMPLE / "artifacts")
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    baseline = output / "baseline.json"
    repeat = output / "repeat.json"
    candidate = output / "candidate.json"

    subprocess.run([str(CLI), "--version"], check=True, env=RUNTIME_ENV, timeout=30)
    with TemporaryDirectory(prefix="playwright-mcp-catalog-") as workdir:
        capture("playwright-mcp-baseline", baseline, Path(workdir))
        capture("playwright-mcp-baseline", repeat, Path(workdir))
        capture("playwright-mcp-candidate", candidate, Path(workdir))

    compare(baseline, repeat, output / "same-version.json", {})
    compare(
        baseline,
        candidate,
        output / "version-upgrade.json",
        {"browser_find": ["inputSchema"], "browser_wait_for": ["inputSchema"]},
    )


if __name__ == "__main__":
    main()
