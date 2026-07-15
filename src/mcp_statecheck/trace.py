"""Versioned, redacted trace artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import string
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from threading import Lock

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

SCHEMA_VERSION = 1
REDACTED = "[REDACTED]"

_SECRET_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "cookie",
    "credential",
    "credentials",
    "passwd",
    "password",
    "privatekey",
    "proxyauthorization",
    "refreshtoken",
    "secret",
    "setcookie",
    "token",
    "xapikey",
}
_SECRET_SUFFIXES = (
    "accesstoken",
    "apikey",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "credential",
    "credentials",
    "passwd",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
)
_SECRET_PARTS = {
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "passwd",
    "password",
    "secret",
    "token",
}
_PUBLIC_PROTOCOL_KEYS = {
    "continuationtoken",
    "nextpagetoken",
    "pagetoken",
    "progresstoken",
    "resumetoken",
}
_SESSION_KEYS = {"httpsessionid", "mcpsessionid", "sessionid"}
_SESSION_TOKEN_CHARACTERS = frozenset(string.ascii_letters + string.digits + "_-")


def _normalized_key(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())


def _key_parts(key: str) -> tuple[str, ...]:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return tuple(
        part for part in re.split(r"[^A-Za-z0-9]+", separated.casefold()) if part
    )


def _has_secret_marker(key: str, *, allow_protocol_keys: bool) -> bool:
    normalized = _normalized_key(key)
    if allow_protocol_keys and normalized in _PUBLIC_PROTOCOL_KEYS:
        return False
    if normalized in _SECRET_KEYS or normalized.endswith(_SECRET_SUFFIXES):
        return True
    parts = _key_parts(key)
    if _SECRET_PARTS.intersection(parts):
        return True
    return any(
        pair in {("api", "key"), ("private", "key"), ("access", "key")}
        for pair in zip(parts, parts[1:], strict=False)
    )


def _is_secret_key(key: str) -> bool:
    return _has_secret_marker(key, allow_protocol_keys=True)


def _is_secret_environment_key(key: str) -> bool:
    return _has_secret_marker(key, allow_protocol_keys=False)


def _is_session_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized in _SESSION_KEYS or normalized.endswith("sessionid")


class _Redactor:
    def __init__(
        self,
        secret_values: Iterable[str] = (),
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if isinstance(secret_values, str):
            secret_values = (secret_values,)
        values = {value for value in secret_values if value}
        if environment is not None:
            values.update(
                value
                for key, value in environment.items()
                if _is_secret_environment_key(key) and value
            )
        self._secret_values = tuple(
            sorted(values, key=lambda value: (-len(value), value))
        )
        self._session_aliases: dict[str, str] = {}
        self._session_values: dict[str, str] = {}

    @staticmethod
    def _session_digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _session_alias(self, value: str) -> str:
        digest = self._session_digest(value)
        alias = self._session_aliases.get(digest)
        if alias is None:
            alias = f"[SESSION_{len(self._session_aliases) + 1}]"
            self._session_aliases[digest] = alias
            self._session_values[value] = alias
        return alias

    def _replace_known_sessions(self, value: str) -> str:
        for session_id, alias in sorted(
            self._session_values.items(), key=lambda item: (-len(item[0]), item[0])
        ):
            start = 0
            pieces: list[str] = []
            while (index := value.find(session_id, start)) >= 0:
                end = index + len(session_id)
                left_is_token = (
                    index > 0 and value[index - 1] in _SESSION_TOKEN_CHARACTERS
                )
                right_is_token = (
                    end < len(value) and value[end] in _SESSION_TOKEN_CHARACTERS
                )
                if left_is_token or right_is_token:
                    pieces.append(value[start:end])
                else:
                    pieces.append(value[start:index])
                    pieces.append(alias)
                start = end
            if pieces:
                pieces.append(value[start:])
                value = "".join(pieces)
        return value

    def _redact_text(self, value: str, *, session: bool = False) -> str:
        if not value:
            return value
        if session:
            return self._session_alias(value)
        value = self._replace_known_sessions(value)
        for secret in self._secret_values:
            value = value.replace(secret, REDACTED)
        return value

    def scrub_known_sessions(self, value: JsonValue) -> JsonValue:
        """Remove known session IDs without discovering new aliases."""

        if isinstance(value, str):
            return self._replace_known_sessions(value)
        if isinstance(value, list):
            return [self.scrub_known_sessions(item) for item in value]
        if isinstance(value, dict):
            return {
                self._replace_known_sessions(key): self.scrub_known_sessions(item)
                for key, item in value.items()
            }
        return value

    def _discover_sessions(self, value: object, *, session: bool = False) -> None:
        if isinstance(value, str):
            if session and value:
                self._session_alias(value)
            return
        if value is None or isinstance(value, bool | int | float):
            if session and value is not None:
                self._session_alias(str(value))
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    continue
                if not _is_secret_key(key):
                    self._discover_sessions(
                        item, session=session or _is_session_key(key)
                    )
            return
        if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            for item in value:
                self._discover_sessions(item, session=session)

    def _redact(self, value: object, *, session: bool = False) -> JsonValue:
        if value is None or isinstance(value, bool | int):
            if session and value is not None:
                return self._session_alias(str(value))
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("trace values must contain finite numbers")
            if session:
                return self._session_alias(str(value))
            return value
        if isinstance(value, str):
            return self._redact_text(value, session=session)
        if isinstance(value, Mapping):
            result: dict[str, JsonValue] = {}
            for key in sorted(value):
                if not isinstance(key, str):
                    raise TypeError("trace object keys must be strings")
                output_key = self._redact_text(key)
                if _is_secret_key(key):
                    result[output_key] = REDACTED
                else:
                    result[output_key] = self._redact(
                        value[key], session=session or _is_session_key(key)
                    )
            return result
        if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            return [self._redact(item, session=session) for item in value]
        raise TypeError(f"unsupported trace value: {type(value).__name__}")

    def redact(self, value: object, *, session: bool = False) -> JsonValue:
        self._discover_sessions(value, session=session)
        return self._redact(value, session=session)


def redact(
    value: object,
    *,
    secret_values: Iterable[str] = (),
    environment: Mapping[str, str] | None = None,
) -> JsonValue:
    """Return a detached JSON value with secrets and session IDs removed."""

    return _Redactor(secret_values, environment).redact(value)


class TraceRecorder:
    """Append canonical actions and normalized events to a redacted trace."""

    def __init__(
        self,
        *,
        protocol_version: str,
        adapter: str,
        sdk_version: str,
        transport: str,
        seed: int,
        secret_values: Iterable[str] = (),
        environment: Mapping[str, str] | None = None,
        fixture_id: str | None = None,
        cleanup: Mapping[str, object] | None = None,
    ) -> None:
        self._lock = Lock()
        self._redactor = _Redactor(secret_values, environment)
        self._metadata: dict[str, JsonValue] = {
            "schema_version": SCHEMA_VERSION,
            "protocol_version": self._redactor.redact(protocol_version),
            "adapter": self._redactor.redact(adapter),
            "sdk_version": self._redactor.redact(sdk_version),
            "transport": self._redactor.redact(transport),
            "seed": self._redactor.redact(seed),
        }
        if fixture_id is not None:
            self._metadata["fixture_id"] = self._redactor.redact(fixture_id)
        if cleanup is not None:
            self._metadata["cleanup"] = self._redactor.redact(cleanup)
        self._canonical_actions: list[dict[str, JsonValue]] = []
        self._normalized_events: list[dict[str, JsonValue]] = []
        self._failure: dict[str, JsonValue] | None = None
        self._next_sequence = 1

    def _record(
        self,
        target: list[dict[str, JsonValue]],
        value: Mapping[str, object],
    ) -> int:
        with self._lock:
            redacted = self._redactor.redact(value)
            if not isinstance(redacted, dict):
                raise TypeError("trace entries must be objects")
            sequence = self._next_sequence
            self._next_sequence += 1
            redacted["sequence"] = sequence
            target.append(redacted)
            return sequence

    def record_action(self, action: Mapping[str, object]) -> int:
        """Append a canonical action and return its trace sequence number."""

        return self._record(self._canonical_actions, action)

    def record_event(self, event: Mapping[str, object]) -> int:
        """Append a normalized event and return its trace sequence number."""

        return self._record(self._normalized_events, event)

    def set_failure(
        self,
        *,
        kind: str,
        spec_reference: str,
        signature: str,
        minimized_reproducer: Sequence[Mapping[str, object]],
    ) -> None:
        """Attach the single failure represented by this trace."""

        with self._lock:
            if self._failure is not None:
                raise RuntimeError("trace failure is already set")
            failure = self._redactor.redact(
                {
                    "kind": kind,
                    "spec_reference": spec_reference,
                    "signature": signature,
                    "minimized_reproducer": list(minimized_reproducer),
                }
            )
            if not isinstance(failure, dict):
                raise AssertionError("failure must be an object")
            self._failure = failure

    def artifact(self) -> dict[str, JsonValue]:
        """Return a detached snapshot of the current artifact."""

        with self._lock:
            artifact = {
                **self._metadata,
                "canonical_actions": self._canonical_actions,
                "normalized_events": self._normalized_events,
            }
            if self._failure is not None:
                artifact["failure"] = self._failure
            snapshot = copy.deepcopy(artifact)
            scrubbed = self._redactor.scrub_known_sessions(snapshot)
            if not isinstance(scrubbed, dict):
                raise AssertionError("trace artifact must be an object")
            return scrubbed

    def write(self, path: str | os.PathLike[str]) -> Path:
        """Atomically write a deterministic JSON artifact."""

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(
                self.artifact(),
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return destination
