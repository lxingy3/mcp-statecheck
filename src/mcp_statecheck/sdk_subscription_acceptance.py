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
from ._subscription_peer import RESOURCE, ControlledSubscriptionHTTPPeer

CHECKED = (
    Path(__file__).resolve().parents[2]
    / "artifacts"
    / "m6-sdk-subscriptions"
    / "acceptance.json"
)
LIFECYCLE_CHECKED = (
    CHECKED.parent.with_name("m6-sdk-subscription-lifecycle") / "acceptance.json"
)
CONCURRENT_CHECKED = (
    CHECKED.parent.with_name("m6-sdk-concurrent-subscriptions") / "acceptance.json"
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
    runner: str,
    transport: str,
    target: list[str] | str,
    runtime: matrix._MatrixRuntime,
    *,
    scenario: str = "graceful",
    recovery_target: list[str] | str | None = None,
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
    probe = {"transport": transport, "target": target}
    if scenario != "graceful":
        probe["scenario"] = scenario
    if scenario == "drop":
        probe["recovery_target"] = recovery_target
    completed = subprocess.run(
        command,
        input=json.dumps(probe),
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
    if scenario == "cancel":
        expected["termination"] = "local"
        expected["replacement"] = {
            key: expected[key] for key in ("events", "honored_filter")
        } | {"termination": "graceful"}
        expected["refetched_tools"] = []
    elif scenario == "drop":
        expected["replacement"] = {**expected, "refetched_tools": []}
        expected["termination"] = "lost" if runner == "python-v2" else "remote"
    elif scenario in {"concurrent-first", "concurrent-second"}:
        cancelled_index = int(scenario == "concurrent-second")
        events = [
            {"type": EXPECTED[runner]["events"][0]},
            {
                "type": "ResourceUpdated"
                if runner == "python-v2"
                else "notifications/resources/updated",
                "uri": RESOURCE,
            },
        ]
        filters = [{"toolsListChanged": True}, {"resourceSubscriptions": [RESOURCE]}]
        expected = {
            key: expected[key]
            for key in (
                "client_closed",
                "protocol_version",
                "runtime_version",
                "sdk_version",
            )
        } | {
            "distinct_ids": True,
            "cancelled_index": cancelled_index,
            "refetched_tools": [],
            "subscriptions": [
                {
                    "honored_filter": filters[index],
                    "before_cancel": [event],
                    "after_cancel": [] if index == cancelled_index else [event],
                    "termination": "local" if index == cancelled_index else "graceful",
                }
                for index, event in enumerate(events)
            ],
        }
    if result != expected:
        raise RuntimeError(
            f"{runner}/{transport}/{scenario} SDK subscription behavior changed: {result}"
        )
    return result


def _run_cells(
    runtime: matrix._MatrixRuntime, *, lifecycle: bool = False, concurrent: bool = False
) -> dict[str, dict]:
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
        scenarios = ("cancel", "drop") if lifecycle else ("graceful",)
        if concurrent:
            scenarios = ("concurrent-first", "concurrent-second")
        for scenario in scenarios:
            mode = {
                "graceful": "conforming",
                "cancel": "held-cancel",
                "drop": "drop-first",
                "concurrent-first": "concurrent",
                "concurrent-second": "concurrent",
            }[scenario]
            suffix = f"/{scenario}" if lifecycle or concurrent else ""
            cells[f"{runner}/stdio{suffix}"] = _adapter(
                runner,
                "stdio",
                [*target[:-1], mode],
                runtime,
                scenario=scenario,
                recovery_target=target,
            )
            with ControlledSubscriptionHTTPPeer(mode) as peer:
                result = _adapter(
                    runner,
                    "streamable-http",
                    peer.url,
                    runtime,
                    scenario=scenario,
                    recovery_target=peer.url,
                )
                if scenario == "cancel" and not peer.cancel_seen.is_set():
                    raise RuntimeError("SDK cancellation did not close its SSE stream")
            if not peer.closed:
                raise RuntimeError(
                    "controlled SDK subscription HTTP peer did not close"
                )
            cells[f"{runner}/streamable-http{suffix}"] = result
    return cells


def run_acceptance(
    output: Path, *, check: bool, lifecycle: bool = False, concurrent: bool = False
) -> dict:
    if lifecycle and concurrent:
        raise ValueError("select only one SDK subscription profile")
    with TemporaryDirectory(prefix="mcp-statecheck-sdk-subscriptions-") as temporary:
        runtime = matrix._materialize_modern_runtime(Path(temporary) / "runners")
        runners = matrix._load_modern_runners(matrix._modern_config())
        matrix._prepare_python(runners, runtime)
        matrix._prepare_typescript(runners, runtime)
        first = _run_cells(runtime, lifecycle=lifecycle, concurrent=concurrent)
        if _run_cells(runtime, lifecycle=lifecycle, concurrent=concurrent) != first:
            raise RuntimeError("SDK subscription campaigns were not byte-identical")
    summary = {
        "schema_version": 1,
        "milestone": "M6.7" if concurrent else "M6.6" if lifecycle else "M6.5",
        "status": "passed",
        "profile": "sdk-native-concurrent-subscriptions"
        if concurrent
        else "sdk-native-subscription-lifecycle"
        if lifecycle
        else "sdk-native-subscriptions",
        "protocol_version": matrix.MODERN_PROTOCOL_VERSION,
        "runs": 2,
        "cells": first,
    }
    content = (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode()
    if check:
        checked = (
            CONCURRENT_CHECKED
            if concurrent
            else LIFECYCLE_CHECKED
            if lifecycle
            else CHECKED
        )
        if checked.read_bytes() != content:
            raise RuntimeError(
                "SDK subscription evidence differs from checked artifact"
            )
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recheck pinned SDK-native subscription evidence"
    )
    parser.add_argument("--check", action="store_true")
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument(
        "--lifecycle",
        action="store_true",
        help="run M6.6 cancellation and reconnect checks",
    )
    profile.add_argument(
        "--concurrent",
        action="store_true",
        help="run M6.7 concurrent subscription isolation checks",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or (
        CONCURRENT_CHECKED
        if args.concurrent
        else LIFECYCLE_CHECKED
        if args.lifecycle
        else CHECKED
    )
    summary = run_acceptance(
        output, check=args.check, lifecycle=args.lifecycle, concurrent=args.concurrent
    )
    print(
        f"{summary['milestone']} SDK subscriptions passed: {len(summary['cells'])} cells, "
        f"{summary['runs']} identical runs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
