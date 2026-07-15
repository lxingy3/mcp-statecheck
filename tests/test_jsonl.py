import pytest

from mcp_statecheck.adapters.jsonl import (
    SCHEMA_VERSION,
    Envelope,
    JsonlError,
    dumps_line,
    iter_envelopes,
    loads_line,
)


def test_envelope_round_trip_is_canonical_and_keeps_ids_independent() -> None:
    assert SCHEMA_VERSION == 1
    envelope = Envelope(
        command_id="ipc-9",
        kind="action",
        payload={"mcp_request_id": 9, "z": 1, "a": 2},
    )
    line = dumps_line(envelope)
    assert line == (
        '{"command_id":"ipc-9","kind":"action",'
        '"payload":{"a":2,"mcp_request_id":9,"z":1},"schema_version":1}\n'
    )
    assert loads_line(line) == envelope
    assert loads_line(line.encode()) == envelope


def test_envelope_from_dict_rejects_non_objects() -> None:
    with pytest.raises(TypeError, match="must be an object"):
        Envelope.from_dict([])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("\n", "blank JSONL"),
        ("{}\n{}\n", "one JSON object"),
        ("[]\n", "must be a JSON object"),
        (
            '{"schema_version":2,"command_id":"c","kind":"k","payload":{}}\n',
            "unsupported schema_version",
        ),
        (
            '{"schema_version":1,"command_id":"c","kind":"k",'
            '"payload":{},"extra":true}\n',
            "unknown field",
        ),
        (
            '{"schema_version":1,"command_id":"a","command_id":"b",'
            '"kind":"k","payload":{}}\n',
            "duplicate object key",
        ),
    ],
)
def test_invalid_jsonl_has_useful_errors(line: str, message: str) -> None:
    with pytest.raises(JsonlError, match=message):
        loads_line(line)


def test_iterator_reports_physical_line_number() -> None:
    good = dumps_line(Envelope("one", "action", {}))
    with pytest.raises(JsonlError, match=r"line 2: blank JSONL"):
        list(iter_envelopes([good, "\n"]))
