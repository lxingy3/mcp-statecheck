from __future__ import annotations

import json
from pathlib import Path

from scripts import run_m4_acceptance


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
