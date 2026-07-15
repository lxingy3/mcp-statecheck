"""Strict versioned JSON Lines envelopes for isolated adapters."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Self

from mcp_statecheck.model import JsonValue, canonical_json

SCHEMA_VERSION = 1


class JsonlError(ValueError):
    """An invalid adapter envelope or JSONL record."""


@dataclass(frozen=True, slots=True)
class Envelope:
    command_id: str
    kind: str
    payload: dict[str, JsonValue]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be an integer")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {self.schema_version}; "
                f"expected {SCHEMA_VERSION}"
            )
        if not isinstance(self.command_id, str) or not self.command_id:
            raise ValueError("command_id must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("kind must be a non-empty string")
        payload = canonical_json(self.payload, where="payload")
        if not isinstance(payload, dict):
            raise TypeError("payload must be an object")
        object.__setattr__(self, "payload", payload)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "command_id": self.command_id,
            "kind": self.kind,
            "payload": canonical_json(self.payload),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        if not isinstance(data, Mapping):
            raise TypeError("envelope must be an object")
        expected = {"command_id", "kind", "payload", "schema_version"}
        missing = expected - data.keys()
        unknown = data.keys() - expected
        if missing:
            raise ValueError(f"missing field(s): {', '.join(sorted(missing))}")
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        payload = data["payload"]
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be an object")
        return cls(
            command_id=data["command_id"],  # type: ignore[arg-type]
            kind=data["kind"],  # type: ignore[arg-type]
            payload=dict(payload),
            schema_version=data["schema_version"],  # type: ignore[arg-type]
        )


def dumps_line(envelope: Envelope) -> str:
    """Serialize one envelope with exactly one trailing newline."""
    return (
        json.dumps(
            envelope.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def loads_line(line: str | bytes, *, line_number: int | None = None) -> Envelope:
    """Parse exactly one JSON object from one physical JSONL line."""
    prefix = f"line {line_number}: " if line_number is not None else ""
    try:
        text = line.decode("utf-8") if isinstance(line, bytes) else line
    except UnicodeDecodeError as exc:
        raise JsonlError(f"{prefix}record is not valid UTF-8") from exc
    if not isinstance(text, str):
        raise JsonlError(f"{prefix}record must be text or UTF-8 bytes")

    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if "\n" in text or "\r" in text:
        raise JsonlError(f"{prefix}expected one JSON object on one physical line")
    if not text.strip():
        raise JsonlError(f"{prefix}blank JSONL records are not allowed")

    try:
        data = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise JsonlError(
            f"{prefix}invalid JSON at column {exc.colno}: {exc.msg}"
        ) from exc
    except ValueError as exc:
        raise JsonlError(f"{prefix}{exc}") from exc
    if not isinstance(data, dict):
        raise JsonlError(f"{prefix}envelope must be a JSON object")
    try:
        return Envelope.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise JsonlError(f"{prefix}invalid envelope: {exc}") from exc


def iter_envelopes(lines: Iterable[str | bytes]) -> Iterable[Envelope]:
    for line_number, line in enumerate(lines, start=1):
        yield loads_line(line, line_number=line_number)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")
