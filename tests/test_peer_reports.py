"""Atomic controlled-peer reports tolerate transient reader contention."""

import errno
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mcp_statecheck import _controlled_peer as peer


def test_stdio_peer_survives_a_transient_report_replacement_denial(
    tmp_path, monkeypatch, capsys
):
    report = tmp_path / "report.json"
    original = os.replace
    denied = []

    def replace_once_locked(source, destination):
        if not denied:
            denied.append(source)
            raise PermissionError("report is held by a reader")
        return original(source, destination)

    monkeypatch.setattr(peer.os, "replace", replace_once_locked)
    monkeypatch.setattr(peer.sys, "stdin", io.StringIO(""))

    assert peer.run_stdio("sdk-modern-smoke", report=report) == 0
    assert json.loads(report.read_text(encoding="utf-8"))["clean_exit"] is True
    assert denied
    assert list(tmp_path.glob("*.tmp")) == []
    assert capsys.readouterr().err == ""


def test_persistent_replacement_denial_preserves_old_report_and_removes_temp(
    tmp_path, monkeypatch
):
    report = tmp_path / "report.json"
    report.write_text('{"old":true}\n', encoding="utf-8")
    replacements = []
    delays = []
    denied = PermissionError("report remains locked")

    def replace_locked(source, destination):
        replacements.append((source, destination, source.read_bytes()))
        raise denied

    monkeypatch.setattr(peer.os, "replace", replace_locked)
    monkeypatch.setattr(peer.time, "sleep", delays.append)

    with pytest.raises(PermissionError) as raised:
        peer._write_json(report, {"new": True})

    assert raised.value is denied
    assert 1 < len(replacements) <= 20
    assert len(delays) == len(replacements) - 1
    assert 0 < sum(delays) < 1
    assert len({source for source, _, _ in replacements}) == 1
    assert len({content for _, _, content in replacements}) == 1
    assert report.read_text(encoding="utf-8") == '{"old":true}\n'
    assert list(tmp_path.glob("*.tmp")) == []


def test_nonpermission_replacement_error_is_not_retried(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text('{"old":true}\n', encoding="utf-8")
    replacements = []
    delays = []
    failure = OSError(errno.EIO, "replacement failed")

    def broken_replace(source, destination):
        replacements.append((source, destination))
        raise failure

    monkeypatch.setattr(peer.os, "replace", broken_replace)
    monkeypatch.setattr(peer.time, "sleep", delays.append)

    with pytest.raises(OSError) as raised:
        peer._write_json(report, {"new": True})

    assert raised.value is failure
    assert len(replacements) == 1
    assert delays == []
    assert report.read_text(encoding="utf-8") == '{"old":true}\n'
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows reader sharing semantics")
def test_windows_report_replacement_recovers_after_a_real_reader_closes(
    tmp_path, monkeypatch
):
    report = tmp_path / "report.json"
    report.write_text('{"old":true}\n', encoding="utf-8")
    original = os.replace
    reader_blocked_replace = threading.Event()
    reader_released = threading.Event()
    denied = []

    def observed_replace(source, destination):
        try:
            return original(source, destination)
        except PermissionError as exc:
            denied.append(exc.winerror)
            reader_blocked_replace.set()
            assert reader_released.wait(timeout=5)
            raise

    monkeypatch.setattr(peer.os, "replace", observed_replace)
    reader = report.open("r", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=1) as executor:
        write = executor.submit(peer._write_json, report, {"new": True})
        try:
            assert reader_blocked_replace.wait(timeout=5)
            assert reader.read() == '{"old":true}\n'
        finally:
            reader.close()
            reader_released.set()
        write.result(timeout=5)

    assert denied and all(code in {5, 32} for code in denied)
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "new": True,
        "pid": os.getpid(),
    }
    assert list(tmp_path.glob("*.tmp")) == []


def test_partial_temporary_write_is_removed_without_replacing_report(
    tmp_path, monkeypatch
):
    report = tmp_path / "report.json"
    report.write_text('{"old":true}\n', encoding="utf-8")
    original = peer.Path.write_text
    replaced = []
    failure = OSError(errno.ENOSPC, "temporary report write failed")

    def partial_write(path, _data, **options):
        original(path, "partial", **options)
        raise failure

    monkeypatch.setattr(peer.Path, "write_text", partial_write)
    monkeypatch.setattr(peer.os, "replace", lambda *args: replaced.append(args))

    with pytest.raises(OSError) as raised:
        peer._write_json(report, {"new": True})

    assert raised.value is failure
    assert replaced == []
    assert report.read_text(encoding="utf-8") == '{"old":true}\n'
    assert list(tmp_path.glob("*.tmp")) == []


def test_temporary_cleanup_failure_preserves_the_replacement_error(
    tmp_path, monkeypatch
):
    report = tmp_path / "report.json"
    failure = OSError(errno.EIO, "replacement failed")

    def broken_replace(*_args):
        raise failure

    def locked_temporary(*_args, **_kwargs):
        raise PermissionError("temporary remains locked")

    monkeypatch.setattr(peer.os, "replace", broken_replace)
    monkeypatch.setattr(peer.Path, "unlink", locked_temporary)

    with pytest.raises(OSError) as raised:
        peer._write_json(report, {"new": True})

    assert raised.value is failure
    assert any("temporary remains locked" in note for note in failure.__notes__)
