from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import run_m4_acceptance


class _FakeProcess:
    pid = 11
    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: int) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -1


def test_acceptance_summary_is_written_atomically(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "acceptance.json"
    summary = {
        "schema_version": 1,
        "milestone": "M4",
        "status": "passed",
    }

    run_m4_acceptance._atomic_write(output, summary)

    assert json.loads(output.read_text(encoding="utf-8")) == summary
    assert not tuple(output.parent.glob(f".{output.name}.*.tmp"))


def test_http_peer_validator_requires_independent_cleanup(tmp_path: Path) -> None:
    report = tmp_path / "http-peer.json"
    payload = {
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
        "negotiated_protocol_version": run_m4_acceptance.PROTOCOL_VERSION,
        "protocol_version_preserved": True,
        "session_preserved": True,
        "pid": 123,
    }
    report.write_text(json.dumps(payload), encoding="utf-8")

    run_m4_acceptance._validate_http_peer(report, 123)

    payload["delete_count"] = 0
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        run_m4_acceptance.AcceptanceError,
        match="headers, session, or cleanup",
    ):
        run_m4_acceptance._validate_http_peer(report, 123)


def test_windows_tree_cleanup_falls_back_for_launcher_and_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[int] = []
    monkeypatch.setattr(run_m4_acceptance.os, "name", "nt")
    monkeypatch.setattr(
        run_m4_acceptance.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", ""),
    )
    monkeypatch.setattr(
        run_m4_acceptance.os,
        "kill",
        lambda pid, _signal: killed.append(pid),
    )

    run_m4_acceptance._stop_process_tree(
        _FakeProcess(),  # type: ignore[arg-type]
        extra_pids=(22,),
    )

    assert killed == [11, 22]
