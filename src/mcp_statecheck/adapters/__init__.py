"""Adapter subprocess protocol primitives."""

from .jsonl import (
    SCHEMA_VERSION,
    Envelope,
    JsonlError,
    dumps_line,
    iter_envelopes,
    loads_line,
)

__all__ = [
    "SCHEMA_VERSION",
    "Envelope",
    "JsonlError",
    "dumps_line",
    "iter_envelopes",
    "loads_line",
]
