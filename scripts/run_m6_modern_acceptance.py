"""Verify the locked M6.1 modern stateless SDK matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

from mcp_statecheck.matrix import _modern_config, run_matrix

if __package__:
    from .run_m4_acceptance import AcceptanceError, _atomic_write
else:
    from run_m4_acceptance import AcceptanceError, _atomic_write

ROOT = Path(__file__).resolve().parents[1]
CHECKED_ARTIFACTS = ROOT / "artifacts" / "m6"
DEFAULT_OUTPUT = Path("artifacts/m6")
PROTOCOL_VERSION = "2026-07-28"
ACTION_PROFILE = "modern-stateless"
TRANSPORTS = ("stdio", "streamable-http")
RUNNERS = {
    "python-v2": {
        "package": "mcp",
        "runtime": "python",
        "runtime_version": "3.12.13",
        "version": "2.1.1",
    },
    "typescript-v2": {
        "package": "@modelcontextprotocol/client",
        "runtime": "node",
        "runtime_version": "24.14.1",
        "version": "2.0.0",
    },
}
RUNS = 3


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary_name)
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _load_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} must contain a JSON object")
    return value


def _locked_config() -> None:
    with _modern_config().open("rb") as handle:
        config = tomllib.load(handle)
    runners = config.get("runners")
    if (
        config.get("schema_version") != 1
        or config.get("action_profile") != ACTION_PROFILE
        or config.get("protocol_versions") != [PROTOCOL_VERSION]
        or tuple(config.get("transports", ())) != TRANSPORTS
        or not isinstance(runners, list)
    ):
        raise AcceptanceError("modern benchmark profile is not exactly locked")
    observed: dict[str, dict[str, object]] = {}
    for runner in runners:
        if not isinstance(runner, Mapping) or not isinstance(runner.get("id"), str):
            raise AcceptanceError("modern benchmark runner is invalid")
        observed[str(runner["id"])] = {
            "package": runner.get("package"),
            "runtime": runner.get("runtime"),
            "version": runner.get("version"),
        }
    expected = {
        runner_id: {
            key: value
            for key, value in runner.items()
            if key in {"package", "runtime", "version"}
        }
        for runner_id, runner in RUNNERS.items()
    }
    if len(runners) != len(RUNNERS) or observed != expected:
        raise AcceptanceError("modern benchmark runner pins changed")


def _expected_names() -> set[Path]:
    return {
        Path(transport) / f"{runner_id}-{PROTOCOL_VERSION}.json"
        for transport in TRANSPORTS
        for runner_id in RUNNERS
    }


def _require_exact_checked_artifacts() -> None:
    expected = {
        Path("acceptance.json"),
        *(Path("matrix") / relative for relative in _expected_names()),
    }
    observed = {
        path.relative_to(CHECKED_ARTIFACTS)
        for path in CHECKED_ARTIFACTS.rglob("*")
        if path.is_file()
    }
    if observed != expected:
        raise AcceptanceError(
            "checked M6.1 artifacts must contain exactly the acceptance summary "
            "and four traces"
        )


def _validate_trace(path: Path, *, runner_id: str, transport: str) -> None:
    trace = _load_object(path, label="modern matrix trace")
    generation = trace.get("generation")
    cleanup = trace.get("cleanup")
    expected_cleanup = (
        {
            "adapter_reaped": True,
            "adapter_returncode": 0,
            "client_closed": True,
            "hang_probe_adapter_reaped": True,
            "hang_probe_peer_reaped": True,
            "peer_clean_exit": True,
            "peer_reaped": True,
        }
        if transport == "stdio"
        else {
            "adapter_reaped": True,
            "adapter_returncode": 0,
            "client_closed": True,
            "hang_probe_adapter_reaped": True,
            "hang_probe_listener_closed": True,
            "listener_closed": True,
            "session_absent": True,
        }
    )
    if (
        trace.get("schema_version") != 1
        or trace.get("adapter") != runner_id
        or trace.get("sdk_version") != RUNNERS[runner_id]["version"]
        or trace.get("protocol_version") != PROTOCOL_VERSION
        or trace.get("transport") != transport
        or trace.get("fixture_id") != "sdk-client-modern-smoke"
        or not isinstance(generation, Mapping)
        or generation.get("action_profile") != ACTION_PROFILE
        or generation.get("runtime_version") != RUNNERS[runner_id]["runtime_version"]
        or cleanup != expected_cleanup
    ):
        raise AcceptanceError("modern matrix trace metadata is invalid")
    actions = trace.get("canonical_actions")
    if (
        not isinstance(actions, list)
        or len(actions) != 5
        or not all(isinstance(action, Mapping) for action in actions)
        or [(action.get("kind"), action.get("method")) for action in actions]
        != [
            ("connect", None),
            ("discover", None),
            ("request", "tools/list"),
            ("request", "tools/call"),
            ("close", None),
        ]
    ):
        raise AcceptanceError("modern matrix trace has the wrong action profile")
    events = trace.get("normalized_events")
    if not isinstance(events, list) or not events:
        raise AcceptanceError("modern matrix trace has no normalized events")
    peer = events[-1]
    if (
        not isinstance(peer, Mapping)
        or peer.get("kind") != "peer_observation"
        or peer.get("request_meta_valid") is not True
        or peer.get("method_order") != ["server/discover", "tools/list", "tools/call"]
    ):
        raise AcceptanceError("modern matrix peer evidence is incomplete")
    if transport == "streamable-http" and any(
        peer.get(key) is not True
        for key in (
            "accept_headers_valid",
            "method_headers_preserved",
            "name_headers_preserved",
            "protocol_headers_preserved",
            "session_absent",
            "standalone_stream_absent",
        )
    ):
        raise AcceptanceError("modern HTTP trace is missing stateless wire evidence")


def _run_once(directory: Path) -> dict[Path, bytes]:
    written = run_matrix(_modern_config(), directory)
    names = {path.relative_to(directory) for path in written}
    expected = _expected_names()
    if len(written) != 4 or names != expected:
        raise AcceptanceError("modern matrix did not write its exact four-cell set")
    payloads: dict[Path, bytes] = {}
    for relative in sorted(expected):
        runner_id = relative.name.removesuffix(f"-{PROTOCOL_VERSION}.json")
        _validate_trace(
            directory / relative,
            runner_id=runner_id,
            transport=relative.parent.as_posix(),
        )
        payloads[relative] = (directory / relative).read_bytes()
    return payloads


def _summary(payloads: Mapping[Path, bytes]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "milestone": "M6.1",
        "slice": "modern-stateless-client-matrix",
        "status": "passed",
        "action_profile": ACTION_PROFILE,
        "protocol_version": PROTOCOL_VERSION,
        "runners": RUNNERS,
        "transports": list(TRANSPORTS),
        "matrix": {
            "cells": len(payloads),
            "runs": RUNS,
            "byte_identical": True,
            "golden_match": True,
            "traces": {
                relative.as_posix(): hashlib.sha256(content).hexdigest()
                for relative, content in sorted(payloads.items())
            },
        },
        "wire_invariants": {
            "initialize_absent": True,
            "request_meta_present": True,
            "sessions_absent": True,
            "standalone_get_absent": True,
            "routable_http_headers_present": True,
        },
        "failure_cleanup": {
            "probes": 4,
            "hard_timeout_seconds": 5,
            "adapter_reaped": True,
            "stdio_peer_reaped": True,
            "http_listener_closed": True,
        },
    }


def run(output: Path, *, check: bool) -> dict[str, object]:
    _locked_config()
    with tempfile.TemporaryDirectory(prefix="mcp-statecheck-m6-modern-") as temporary:
        work = Path(temporary)
        attempts = [
            _run_once(work / f"run-{index:02d}") for index in range(1, RUNS + 1)
        ]
        first = attempts[0]
        if any(attempt != first for attempt in attempts[1:]):
            raise AcceptanceError("modern matrix runs were not byte-identical")

        summary = _summary(first)
        generated_summary = work / "acceptance.json"
        _atomic_write(generated_summary, summary)
        if check:
            _require_exact_checked_artifacts()
            for relative, content in first.items():
                try:
                    expected = (CHECKED_ARTIFACTS / "matrix" / relative).read_bytes()
                except OSError as exc:
                    raise AcceptanceError(
                        f"checked modern trace is missing: {relative.as_posix()}"
                    ) from exc
                if content != expected:
                    raise AcceptanceError(
                        f"modern trace differs from checked evidence: {relative.as_posix()}"
                    )
            try:
                expected_summary = (CHECKED_ARTIFACTS / "acceptance.json").read_bytes()
            except OSError as exc:
                raise AcceptanceError(
                    "checked modern acceptance summary is missing"
                ) from exc
            if generated_summary.read_bytes() != expected_summary:
                raise AcceptanceError(
                    "modern acceptance summary differs from checked evidence"
                )

        for relative, content in first.items():
            source = work / "run-01" / relative
            if source.read_bytes() != content:
                raise AcceptanceError("modern matrix evidence changed before copy")
            _copy_file(source, output / "matrix" / relative)
        _copy_file(generated_summary, output / "acceptance.json")
        return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the M6.1 modern stateless SDK matrix three times."
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare all generated evidence with the checked M6.1 artifacts",
    )
    args = parser.parse_args(argv)
    try:
        if args.check and args.output is None:
            with tempfile.TemporaryDirectory(
                prefix="mcp-statecheck-m6-modern-check-"
            ) as temporary:
                summary = run(Path(temporary), check=True)
            destination = "checked-in evidence"
        else:
            output = args.output or DEFAULT_OUTPUT
            summary = run(output, check=args.check)
            destination = str(output)
    except (AcceptanceError, OSError, TypeError, ValueError) as exc:
        print(f"M6.1 modern acceptance failed: {exc}", file=sys.stderr)
        return 2
    matrix = summary["matrix"]
    assert isinstance(matrix, Mapping)
    print(
        "M6.1 modern acceptance passed: "
        f"{matrix['cells']} cells across {matrix['runs']} byte-identical runs; "
        f"{destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
