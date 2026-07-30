"""Deterministic, offline projections of versioned trace artifacts."""

from __future__ import annotations

import html
import json
import os
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .model import canonical_json
from .trace import SCHEMA_VERSION, JsonValue, redact

type Artifact = dict[str, JsonValue]
type PathLike = str | os.PathLike[str]

_REQUIRED_FIELDS = {
    "adapter",
    "canonical_actions",
    "normalized_events",
    "protocol_version",
    "schema_version",
    "sdk_version",
    "seed",
    "transport",
}
_SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
_PROJECT_URL = "https://github.com/lxingy3/mcp-statecheck"


class ReportError(ValueError):
    """A trace cannot be loaded or projected safely."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite number")


def _prepare_artifact(
    artifact: object,
    *,
    secret_values: Iterable[str] = (),
    environment: Mapping[str, str] | None = None,
) -> Artifact:
    try:
        normalized = canonical_json(artifact, where="artifact")
        prepared = redact(
            normalized,
            secret_values=secret_values,
            environment=environment,
        )
    except (TypeError, ValueError) as exc:
        raise ReportError("artifact contains unsupported values") from exc
    if not isinstance(prepared, dict):
        raise ReportError("artifact must be a JSON object")

    missing = _REQUIRED_FIELDS - prepared.keys()
    if missing:
        raise ReportError(
            f"artifact is missing required field(s): {', '.join(sorted(missing))}"
        )
    if type(prepared["schema_version"]) is not int:
        raise ReportError("artifact schema_version must be an integer")
    if prepared["schema_version"] != SCHEMA_VERSION:
        raise ReportError(
            f"unsupported artifact schema_version; expected {SCHEMA_VERSION}"
        )
    for name in ("protocol_version", "adapter", "sdk_version", "transport"):
        if not isinstance(prepared[name], str) or not prepared[name]:
            raise ReportError(f"artifact {name} must be a non-empty string")
    if type(prepared["seed"]) is not int:
        raise ReportError("artifact seed must be an integer")

    seen_sequences: set[int] = set()
    for name in ("canonical_actions", "normalized_events"):
        entries = prepared[name]
        if not isinstance(entries, list):
            raise ReportError(f"artifact {name} must be an array")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ReportError(f"artifact {name} must contain objects")
            sequence = entry.get("sequence")
            if type(sequence) is not int or sequence <= 0:
                raise ReportError(f"artifact {name} contains an invalid sequence")
            if sequence in seen_sequences:
                raise ReportError("artifact contains duplicate trace sequences")
            seen_sequences.add(sequence)

    fixture_id = prepared.get("fixture_id")
    if fixture_id is not None and (not isinstance(fixture_id, str) or not fixture_id):
        raise ReportError("artifact fixture_id must be a non-empty string")
    for name in ("cleanup", "generation", "replay", "target_recipe"):
        value = prepared.get(name)
        if value is not None and not isinstance(value, dict):
            raise ReportError(f"artifact {name} must be an object")

    if "failure" in prepared:
        failure = prepared["failure"]
        if not isinstance(failure, dict):
            raise ReportError("artifact failure must be an object when present")
        required_failure_fields = {
            "kind",
            "minimized_reproducer",
            "signature",
            "spec_reference",
        }
        missing_failure_fields = required_failure_fields - failure.keys()
        if missing_failure_fields:
            raise ReportError(
                "artifact failure is missing required field(s): "
                + ", ".join(sorted(missing_failure_fields))
            )
        for name in ("kind", "signature", "spec_reference"):
            if not isinstance(failure[name], str) or not failure[name]:
                raise ReportError(f"artifact failure {name} must be a non-empty string")
        reproducer = failure["minimized_reproducer"]
        if not isinstance(reproducer, list) or not all(
            isinstance(action, dict) for action in reproducer
        ):
            raise ReportError(
                "artifact failure minimized_reproducer must contain objects"
            )
        trigger = failure.get("trigger_action_id")
        if trigger is not None and (not isinstance(trigger, str) or not trigger):
            raise ReportError(
                "artifact failure trigger_action_id must be a non-empty string"
            )
        evidence = failure.get("evidence")
        if evidence is not None and not isinstance(evidence, dict):
            raise ReportError("artifact failure evidence must be an object")
    return prepared


def load_artifact(
    path: PathLike,
    *,
    secret_values: Iterable[str] = (),
    environment: Mapping[str, str] | None = None,
) -> Artifact:
    """Load, validate, and redact one trace artifact."""

    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ReportError(f"artifact is not valid UTF-8: {source}") from exc
    except OSError as exc:
        raise ReportError(f"could not read artifact: {source}") from exc
    try:
        artifact = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ReportError(
            f"invalid JSON artifact at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except ValueError as exc:
        raise ReportError("invalid JSON artifact") from exc
    return _prepare_artifact(
        artifact,
        secret_values=secret_values,
        environment=environment,
    )


def _artifact_status(artifact: Artifact) -> str:
    if "failure" in artifact:
        return "failure"
    generation = artifact.get("generation")
    if (
        isinstance(generation, dict)
        and generation.get("outcome") == "infrastructure_error"
    ):
        return "infrastructure_error"
    return "passed"


def artifact_status(
    artifact: object,
    *,
    secret_values: Iterable[str] = (),
    environment: Mapping[str, str] | None = None,
) -> str:
    """Return passed, failure, or infrastructure_error for one artifact."""

    return _artifact_status(
        _prepare_artifact(
            artifact,
            secret_values=secret_values,
            environment=environment,
        )
    )


def _json_text(value: object) -> str:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render_json(
    artifact: object,
    *,
    secret_values: Iterable[str] = (),
    environment: Mapping[str, str] | None = None,
) -> str:
    """Render the canonical JSON report."""

    return _json_text(
        _prepare_artifact(
            artifact,
            secret_values=secret_values,
            environment=environment,
        )
    )


def _xml_text(value: object) -> str:
    text = str(value)
    return "".join(
        character
        if (
            ord(character) in (0x09, 0x0A, 0x0D)
            or 0x20 <= ord(character) <= 0xD7FF
            or 0xE000 <= ord(character) <= 0xFFFD
            or 0x10000 <= ord(character) <= 0x10FFFF
        )
        else "\ufffd"
        for character in text
    )


def _render_junit(artifact: Artifact) -> str:
    status = _artifact_status(artifact)
    failure = artifact.get("failure")
    failed = status == "failure"
    infrastructure_error = status == "infrastructure_error"
    suite = ET.Element(
        "testsuite",
        {
            "errors": "1" if infrastructure_error else "0",
            "failures": "1" if failed else "0",
            "name": "mcp-statecheck",
            "skipped": "0",
            "tests": "1",
        },
    )
    properties = ET.SubElement(suite, "properties")
    metadata = (
        ("protocol_version", artifact["protocol_version"]),
        ("adapter", artifact["adapter"]),
        ("sdk_version", artifact["sdk_version"]),
        ("transport", artifact["transport"]),
        ("seed", artifact["seed"]),
    )
    for name, value in metadata:
        ET.SubElement(
            properties,
            "property",
            {"name": name, "value": _xml_text(value)},
        )
    fixture_id = artifact.get("fixture_id")
    if fixture_id is not None:
        ET.SubElement(
            properties,
            "property",
            {"name": "fixture_id", "value": _xml_text(fixture_id)},
        )

    case_name = fixture_id if isinstance(fixture_id, str) else "trace"
    case = ET.SubElement(
        suite,
        "testcase",
        {
            "classname": _xml_text(
                f"mcp-statecheck.{artifact['transport']}.{artifact['adapter']}"
            ),
            "name": _xml_text(case_name),
        },
    )
    if failed:
        kind = failure["kind"]
        signature = failure["signature"]
        spec_reference = failure["spec_reference"]
        failed_case = ET.SubElement(
            case,
            "failure",
            {
                "message": _xml_text(kind),
                "type": _xml_text(kind),
            },
        )
        failed_case.text = _xml_text(
            f"Signature: {signature}\n"
            f"Spec: {spec_reference}\n"
            "Minimized reproducer:\n"
            + json.dumps(
                failure["minimized_reproducer"],
                allow_nan=False,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
    elif infrastructure_error:
        message = next(
            (
                event.get("message")
                for event in reversed(artifact["normalized_events"])
                if isinstance(event, dict)
                and event.get("kind") == "infrastructure_error"
            ),
            "mcp-statecheck could not complete the check",
        )
        error = ET.SubElement(
            case,
            "error",
            {
                "message": _xml_text(message),
                "type": "infrastructure_error",
            },
        )
        error.text = _xml_text(message)

    ET.indent(suite, space="  ")
    return (
        ET.tostring(
            suite,
            encoding="unicode",
            xml_declaration=True,
            short_empty_elements=True,
        )
        + "\n"
    )


def render_junit(
    artifact: object,
    *,
    secret_values: Iterable[str] = (),
    environment: Mapping[str, str] | None = None,
) -> str:
    """Render one trace as a deterministic JUnit XML suite."""

    return _render_junit(
        _prepare_artifact(
            artifact,
            secret_values=secret_values,
            environment=environment,
        )
    )


def _safe_help_uri(value: str) -> str | None:
    if any(
        ord(character) < 0x20 or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _render_sarif(artifact: Artifact) -> str:
    failure = artifact.get("failure")
    rules: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    if isinstance(failure, dict):
        kind = str(failure["kind"])
        signature = str(failure["signature"])
        spec_reference = str(failure["spec_reference"])
        rule: dict[str, object] = {
            "defaultConfiguration": {"level": "error"},
            "fullDescription": {
                "text": f"MCP protocol or differential failure: {kind}"
            },
            "id": kind,
            "shortDescription": {"text": kind},
        }
        help_uri = _safe_help_uri(spec_reference)
        if help_uri is not None:
            rule["helpUri"] = help_uri
        rules.append(rule)
        properties: dict[str, object] = {
            "adapter": artifact["adapter"],
            "protocolVersion": artifact["protocol_version"],
            "sdkVersion": artifact["sdk_version"],
            "signature": signature,
            "specReference": spec_reference,
            "transport": artifact["transport"],
        }
        fixture_id = artifact.get("fixture_id")
        if fixture_id is not None:
            properties["fixtureId"] = fixture_id
        results.append(
            {
                "level": "error",
                "message": {
                    "text": f"mcp-statecheck detected {kind}. Signature: {signature}."
                },
                "partialFingerprints": {
                    "mcpStatecheckSignature/v1": signature,
                },
                "properties": properties,
                "ruleId": kind,
                "ruleIndex": 0,
            }
        )
    return _json_text(
        {
            "$schema": _SARIF_SCHEMA,
            "runs": [
                {
                    "invocations": [
                        {
                            "executionSuccessful": (
                                _artifact_status(artifact) != "infrastructure_error"
                            )
                        }
                    ],
                    "results": results,
                    "tool": {
                        "driver": {
                            "informationUri": _PROJECT_URL,
                            "name": "mcp-statecheck",
                            "rules": rules,
                            "version": __version__,
                        }
                    },
                }
            ],
            "version": "2.1.0",
        }
    )


def render_sarif(
    artifact: object,
    *,
    secret_values: Iterable[str] = (),
    environment: Mapping[str, str] | None = None,
) -> str:
    """Render one trace as SARIF 2.1.0 without invented source locations."""

    return _render_sarif(
        _prepare_artifact(
            artifact,
            secret_values=secret_values,
            environment=environment,
        )
    )


def _html_text(value: object) -> str:
    return html.escape(_xml_text(value), quote=True)


def _html_json(value: object) -> str:
    return html.escape(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ),
        quote=True,
    )


def _render_html(artifact: Artifact) -> str:
    artifact_status = _artifact_status(artifact)
    failure = artifact.get("failure")
    failed = artifact_status == "failure"
    if artifact_status == "infrastructure_error":
        status = "Check did not complete"
        status_class = "error"
    elif failed:
        status = "Failure detected"
        status_class = "fail"
    else:
        status = "No failure detected"
        status_class = "pass"
    fixture_id = artifact.get("fixture_id", "trace")
    timeline = [
        ("Action", entry)
        for entry in artifact["canonical_actions"]  # type: ignore[union-attr]
    ] + [
        ("Event", entry)
        for entry in artifact["normalized_events"]  # type: ignore[union-attr]
    ]
    timeline.sort(key=lambda item: item[1]["sequence"])

    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" '
        'content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; '
        'base-uri &#39;none&#39;; form-action &#39;none&#39;">',
        f"<title>mcp-statecheck · {_html_text(fixture_id)}</title>",
        "<style>",
        ":root{color-scheme:light dark;font:15px/1.5 system-ui,sans-serif;"
        "--bg:#f4f7fb;--card:#fff;--text:#172033;--muted:#60708a;"
        "--line:#d9e1ec;--pass:#08783e;--fail:#b42318;--code:#f0f3f8}",
        "@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#161b22;"
        "--text:#e6edf3;--muted:#9da7b3;--line:#30363d;--pass:#3fb950;"
        "--fail:#ff7b72;--code:#0d1117}}",
        "*{box-sizing:border-box}body{margin:0;background:var(--bg);"
        "color:var(--text)}main{max-width:1080px;margin:auto;padding:40px 20px 64px}"
        "header,.card,details{background:var(--card);border:1px solid var(--line);"
        "border-radius:12px}header,.card{padding:22px;margin-bottom:18px}"
        "h1,h2{margin:0 0 10px}h1{font-size:1.75rem}h2{font-size:1.1rem}"
        ".status{font-weight:700}.pass{color:var(--pass)}"
        ".fail,.error{color:var(--fail)}"
        ".meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));"
        "gap:12px;margin-top:18px}.meta div{min-width:0}.label{color:var(--muted);"
        "font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}"
        ".value{overflow-wrap:anywhere}details{margin:10px 0;overflow:hidden}"
        "summary{cursor:pointer;padding:13px 16px;font-weight:650}"
        "pre{margin:0;padding:16px;overflow:auto;background:var(--code);"
        "border-top:1px solid var(--line);font:12.5px/1.55 ui-monospace,monospace}"
        "code{overflow-wrap:anywhere}.sequence{display:inline-block;color:var(--muted);"
        "width:3.5em}footer{color:var(--muted);text-align:center;margin-top:24px;"
        "font-size:.85rem}",
        "</style>",
        "</head>",
        "<body>",
        "<main>",
        "<header>",
        "<h1>mcp-statecheck trace</h1>",
        f'<div class="status {status_class}">{status}</div>',
        '<div class="meta">',
    ]
    for label, value in (
        ("Fixture", fixture_id),
        ("Protocol", artifact["protocol_version"]),
        ("Adapter", artifact["adapter"]),
        ("SDK", artifact["sdk_version"]),
        ("Transport", artifact["transport"]),
        ("Seed", artifact["seed"]),
    ):
        parts.append(
            "<div>"
            f'<div class="label">{_html_text(label)}</div>'
            f'<div class="value">{_html_text(value)}</div>'
            "</div>"
        )
    parts.extend(["</div>", "</header>"])

    if failed:
        parts.extend(
            [
                '<section class="card">',
                "<h2>Failure</h2>",
                f"<p><strong>Kind:</strong> <code>{_html_text(failure['kind'])}</code></p>",
                f"<p><strong>Signature:</strong> "
                f"<code>{_html_text(failure['signature'])}</code></p>",
                f"<p><strong>Spec:</strong> "
                f"<code>{_html_text(failure['spec_reference'])}</code></p>",
                "<details open>",
                "<summary>Minimized reproducer</summary>",
                f"<pre>{_html_json(failure['minimized_reproducer'])}</pre>",
                "</details>",
                "</section>",
            ]
        )
    elif artifact_status == "infrastructure_error":
        message = next(
            (
                event.get("message")
                for event in reversed(artifact["normalized_events"])
                if isinstance(event, dict)
                and event.get("kind") == "infrastructure_error"
            ),
            "mcp-statecheck could not complete the check",
        )
        parts.extend(
            [
                '<section class="card">',
                "<h2>Infrastructure error</h2>",
                f"<p>{_html_text(message)}</p>",
                "</section>",
            ]
        )

    parts.extend(['<section class="card">', "<h2>Timeline</h2>"])
    for entry_type, entry in timeline:
        kind = entry.get("kind", "unknown")
        detail = (
            entry.get("method")
            or entry.get("action_id")
            or entry.get("target_action_id")
            or ""
        )
        parts.extend(
            [
                f'<details class="{entry_type.casefold()}">',
                "<summary>"
                f'<span class="sequence">#{entry["sequence"]}</span>'
                f"{_html_text(entry_type)} · {_html_text(kind)}"
                + (f" · {_html_text(detail)}" if detail else "")
                + "</summary>",
                f"<pre>{_html_json(entry)}</pre>",
                "</details>",
            ]
        )
    parts.extend(
        [
            "</section>",
            '<section class="card">',
            "<h2>Raw artifact</h2>",
            "<details>",
            "<summary>Versioned JSON</summary>",
            f"<pre>{_html_json(artifact)}</pre>",
            "</details>",
            "</section>",
            "<footer>Generated offline by mcp-statecheck.</footer>",
            "</main>",
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(parts) + "\n"


def render_html(
    artifact: object,
    *,
    secret_values: Iterable[str] = (),
    environment: Mapping[str, str] | None = None,
) -> str:
    """Render one self-contained, script-free HTML trace explorer."""

    return _render_html(
        _prepare_artifact(
            artifact,
            secret_values=secret_values,
            environment=environment,
        )
    )


def _same_path(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def validate_output_paths(
    *,
    source_path: PathLike | None = None,
    json_path: PathLike | None = None,
    junit_path: PathLike | None = None,
    sarif_path: PathLike | None = None,
    html_path: PathLike | None = None,
) -> None:
    """Reject source/output aliases before any evidence is written."""

    destinations = [
        Path(path)
        for path in (json_path, junit_path, sarif_path, html_path)
        if path is not None
    ]
    source = Path(source_path) if source_path is not None else None
    for index, destination in enumerate(destinations):
        if source is not None and _same_path(source, destination):
            raise ReportError("report output must not overwrite its source artifact")
        if any(_same_path(destination, other) for other in destinations[:index]):
            raise ReportError("report output paths must be distinct")


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_reports(
    artifact: object,
    *,
    source_path: PathLike | None = None,
    json_path: PathLike | None = None,
    junit_path: PathLike | None = None,
    sarif_path: PathLike | None = None,
    html_path: PathLike | None = None,
    secret_values: Iterable[str] = (),
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Atomically write requested projections without overwriting their source."""

    prepared = _prepare_artifact(
        artifact,
        secret_values=secret_values,
        environment=environment,
    )
    destinations = [
        (name, Path(path))
        for name, path in (
            ("json", json_path),
            ("junit", junit_path),
            ("sarif", sarif_path),
            ("html", html_path),
        )
        if path is not None
    ]
    validate_output_paths(
        source_path=source_path,
        json_path=json_path,
        junit_path=junit_path,
        sarif_path=sarif_path,
        html_path=html_path,
    )

    renderers = {
        "json": lambda: _json_text(prepared),
        "junit": lambda: _render_junit(prepared),
        "sarif": lambda: _render_sarif(prepared),
        "html": lambda: _render_html(prepared),
    }
    payloads = [(destination, renderers[name]()) for name, destination in destinations]
    written: list[Path] = []
    for destination, payload in payloads:
        try:
            _atomic_write(destination, payload)
        except OSError as exc:
            raise ReportError(f"could not write report: {destination}") from exc
        written.append(destination)
    return tuple(written)
