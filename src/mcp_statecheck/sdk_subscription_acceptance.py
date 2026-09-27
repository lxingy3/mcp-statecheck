"""Real pinned SDK clients against controlled modern subscription peers."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from . import matrix
from ._subscription_peer import ControlledSubscriptionHTTPPeer

CHECKED = (
    Path(__file__).resolve().parents[2]
    / "artifacts"
    / "m6-sdk-subscriptions"
    / "acceptance.json"
)
RUNNERS = ("python-v2", "typescript-v2")
TRANSPORTS = ("stdio", "streamable-http")
EXPECTED = {
    "python-v2": {
        "sdk_version": "2.1.1",
        "runtime_version": "3.12.13",
        "events": ["ToolsListChanged"],
    },
    "typescript-v2": {
        "sdk_version": "2.0.0",
        "runtime_version": matrix.NODE_VERSION,
        "events": ["notifications/tools/list_changed"],
    },
}


def _adapter(
    runner: str, transport: str, target: list[str] | str, runtime: matrix._MatrixRuntime
) -> dict:
    environment = matrix._isolated_environment()
    if runner == "python-v2":
        python = (
            runtime.python_environments[runner]
            / ".venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        command = [
            str(python),
            "-I",
            str(matrix.ASSET_ROOT / "python_subscription_client.py"),
        ]
    else:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError(
                "Node.js is required for the TypeScript subscription probe"
            )
        command = [node, str(matrix.ASSET_ROOT / "typescript_subscription_client.mts")]
        environment["MCP_STATECHECK_NODE_ENV"] = str(
            runtime.typescript_environments[runner]
        )
    completed = subprocess.run(
        command,
        input=json.dumps({"transport": transport, "target": target}),
        capture_output=True,
        cwd=runtime.workdir,
        env=environment,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{runner}/{transport} SDK probe failed: {completed.stderr.strip()}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{runner}/{transport} returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"{runner}/{transport} returned invalid evidence")
    expected = {
        "client_closed": True,
        "events": EXPECTED[runner]["events"],
        "honored_filter": {"toolsListChanged": True},
        "protocol_version": matrix.MODERN_PROTOCOL_VERSION,
        "runtime_version": EXPECTED[runner]["runtime_version"],
        "sdk_version": EXPECTED[runner]["sdk_version"],
        "termination": "graceful",
    }
    if result != expected:
        raise RuntimeError(
            f"{runner}/{transport} SDK subscription behavior changed: {result}"
        )
    return result


def _run_cells(runtime: matrix._MatrixRuntime) -> dict[str, dict]:
    cells: dict[str, dict] = {}
    for runner in RUNNERS:
        target = [
            sys.executable,
            "-I",
            "-m",
            "mcp_statecheck._subscription_peer",
            "--mode",
            "conforming",
        ]
        cells[f"{runner}/stdio"] = _adapter(runner, "stdio", target, runtime)
        with ControlledSubscriptionHTTPPeer("conforming") as peer:
            result = _adapter(runner, "streamable-http", peer.url, runtime)
        if not peer.closed:
            raise RuntimeError("controlled SDK subscription HTTP peer did not close")
        cells[f"{runner}/streamable-http"] = result
    return cells


def run_acceptance(output: Path, *, check: bool) -> dict:
    with TemporaryDirectory(prefix="mcp-statecheck-sdk-subscriptions-") as temporary:
        runtime = matrix._materialize_modern_runtime(Path(temporary) / "runners")
        runners = matrix._load_modern_runners(matrix._modern_config())
        matrix._prepare_python(runners, runtime)
        matrix._prepare_typescript(runners, runtime)
        first = _run_cells(runtime)
        if _run_cells(runtime) != first:
            raise RuntimeError("SDK subscription campaigns were not byte-identical")
    summary = {
        "schema_version": 1,
        "milestone": "M6.5",
        "status": "passed",
        "profile": "sdk-native-subscriptions",
        "protocol_version": matrix.MODERN_PROTOCOL_VERSION,
        "runs": 2,
        "cells": first,
    }
    content = (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode()
    if check:
        if CHECKED.read_bytes() != content:
            raise RuntimeError(
                "SDK subscription evidence differs from checked artifact"
            )
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recheck M6.5 pinned SDK-native subscription evidence"
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=CHECKED)
    args = parser.parse_args()
    summary = run_acceptance(args.output, check=args.check)
    print(
        f"M6.5 SDK subscriptions passed: {len(summary['cells'])} cells, "
        f"{summary['runs']} identical runs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
