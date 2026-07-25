"""Run the five controlled M1 fixtures and write their real wire traces."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from mcp_statecheck.fixtures import FIXTURES


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/m1"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    expected_traces = [f"{fixture.fixture_id}.json" for fixture in FIXTURES]
    for name in expected_traces:
        (args.output / name).unlink(missing_ok=True)

    environment = os.environ.copy()
    environment["MCP_STATECHECK_ARTIFACT_DIR"] = str(args.output.resolve())
    command = [sys.executable, "-m", "pytest", "-q"]
    timeout_seconds = 180
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env=environment,
            text=True,
            timeout=timeout_seconds,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = 2
        stdout = (
            exc.stdout.decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else exc.stdout or ""
        )
        stderr_text = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else exc.stderr or ""
        )
        stderr = stderr_text + "M1 acceptance exceeded its hard timeout\n"

    generated_traces = sorted(
        name for name in expected_traces if (args.output / name).is_file()
    )
    passed_match = re.search(r"(\d+) passed", stdout)
    tests_passed = int(passed_match.group(1)) if passed_match else 0
    if returncode == 0 and (
        not tests_passed or len(generated_traces) != len(expected_traces)
    ):
        returncode = 2
        stderr += "M1 acceptance output was incomplete\n"

    summary = {
        "schema_version": 1,
        "milestone": "M1",
        "status": "passed" if returncode == 0 else "failed",
        "suite": "full",
        "python": sys.version.split()[0],
        "command": command[1:],
        "timeout_seconds": timeout_seconds,
        "tests_passed": tests_passed if returncode == 0 else 0,
        "fixtures": [fixture.fixture_id for fixture in FIXTURES],
        "fixture_checks_passed": len(generated_traces) if returncode == 0 else 0,
        "generated_traces": generated_traces,
    }
    (args.output / "acceptance.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if returncode:
        sys.stderr.write(stdout)
        sys.stderr.write(stderr)
    else:
        print(
            f"M1 acceptance passed: {tests_passed} tests and "
            f"{len(generated_traces)} fixture traces written to {args.output}"
        )
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
