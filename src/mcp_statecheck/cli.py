"""Command-line entry point for checking MCP servers and rendering reports."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import httpx

from . import __version__
from .execution import (
    ExecutionProtocolError,
    ExecutionResult,
    execute_http,
    execute_stdio,
)
from .invariants import Failure, failure_signature
from .model import PROTOCOL_VERSIONS, Action, ActionKind, JsonValue
from .reports import (
    ReportError,
    artifact_status,
    load_artifact,
    validate_output_paths,
    write_reports,
)
from .trace import TraceRecorder, redact
from .transports import (
    HTTPProtocolError,
    HTTPTimeout,
    HTTPTransportError,
    StdioError,
    StdioProtocolError,
    StdioTimeout,
)

DEFAULT_PROTOCOL_VERSION = PROTOCOL_VERSIONS[-1]
DEFAULT_ARTIFACT = Path("artifacts/run.json")
DEFAULT_MATRIX_OUTPUT = Path("artifacts/matrix")
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HEADER_VALUE = re.compile(r"^[\t\x20-\x7e]+$")


def _positive_float(value: str) -> float:
    number = float(value)
    if not isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-statecheck",
        description="Protocol smoke checks and offline reports for MCP implementations.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    check = subparsers.add_parser(
        "check",
        help="run the quick protocol smoke check against one explicit server",
    )
    target = check.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--stdio",
        action="store_true",
        help="start the server command following a required -- delimiter",
    )
    target.add_argument("--url", help="explicit Streamable HTTP endpoint")
    check.add_argument(
        "--header-env",
        action="append",
        default=[],
        metavar="HEADER=ENV",
        help="read an HTTP header value from an environment variable",
    )
    check.add_argument(
        "--protocol-version",
        "--protocol",
        choices=PROTOCOL_VERSIONS,
        default=DEFAULT_PROTOCOL_VERSION,
    )
    check.add_argument("--timeout", type=_positive_float, default=5.0)
    check.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT)
    _add_report_outputs(check)
    check.add_argument("server_command", nargs=argparse.REMAINDER)

    report = subparsers.add_parser(
        "report",
        help="render one saved artifact without network access",
    )
    report.add_argument("artifact", type=Path)
    report.add_argument("--json", type=Path, metavar="PATH")
    _add_report_outputs(report)

    matrix = subparsers.add_parser(
        "matrix",
        help="run the locked Python and TypeScript SDK transport matrix",
    )
    matrix.add_argument("config", nargs="?", type=Path)
    matrix.add_argument("--output", type=Path, default=DEFAULT_MATRIX_OUTPUT)
    matrix.add_argument(
        "--check",
        action="store_true",
        help="compare generated traces with the explicit output directory",
    )
    return parser


def _add_report_outputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--junit", type=Path, metavar="PATH")
    parser.add_argument("--sarif", type=Path, metavar="PATH")
    parser.add_argument("--html", type=Path, metavar="PATH")


def _smoke_actions(protocol_version: str) -> tuple[Action, ...]:
    return (
        Action(
            "initialize",
            ActionKind.INITIALIZE,
            mcp_request_id=1,
            protocol_version=protocol_version,
            capabilities={},
        ),
        Action(
            "initialize-response",
            ActionKind.RESPONSE,
            target_action_id="initialize",
        ),
        Action("initialized", ActionKind.INITIALIZED),
        Action(
            "ping",
            ActionKind.REQUEST,
            mcp_request_id=2,
            method="ping",
            payload={},
        ),
        Action(
            "ping-response",
            ActionKind.RESPONSE,
            target_action_id="ping",
        ),
        Action(
            "tools-list",
            ActionKind.REQUEST,
            mcp_request_id=3,
            method="tools/list",
            payload={},
        ),
        Action(
            "tools-list-response",
            ActionKind.RESPONSE,
            target_action_id="tools-list",
        ),
    )


def _header_values(
    assignments: Sequence[str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    headers: dict[str, str] = {}
    seen: set[str] = set()
    secrets: list[str] = []
    for assignment in assignments:
        if "=" not in assignment:
            raise ValueError("header environment reference must be HEADER=ENV")
        name, variable = assignment.split("=", 1)
        if not _HEADER_NAME.fullmatch(name) or not variable:
            raise ValueError("header environment reference must be HEADER=ENV")
        folded = name.casefold()
        if folded in seen:
            raise ValueError(f"duplicate HTTP header: {name}")
        value = os.environ.get(variable)
        if value is None or not value:
            raise ValueError(f"environment variable is missing or empty: {variable}")
        if not _HEADER_VALUE.fullmatch(value):
            raise ValueError(f"environment variable is not a valid header: {variable}")
        seen.add(folded)
        headers[name] = value
        secrets.append(value)
    return headers, tuple(secrets)


def _validated_target(
    args: argparse.Namespace,
) -> tuple[str, tuple[str, ...] | str, dict[str, str], tuple[str, ...]]:
    command = tuple(args.server_command)
    if args.stdio:
        if args.header_env:
            raise ValueError("--header-env is only valid with --url")
        if not command or command[0] != "--" or len(command) == 1:
            raise ValueError("--stdio requires -- followed by a server command")
        return "stdio", command[1:], {}, ()

    if command:
        raise ValueError("--url does not accept a server command")
    invalid_url = "--url must be an HTTP(S) endpoint without credentials or a fragment"
    try:
        parsed = urlsplit(args.url)
        http_url = httpx.URL(args.url)
        _ = parsed.port
    except (ValueError, httpx.InvalidURL) as exc:
        raise ValueError(invalid_url) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or not http_url.host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(invalid_url)
    headers, secrets = _header_values(args.header_env)
    return "streamable-http", args.url, headers, secrets


async def _execute_check(
    actions: tuple[Action, ...],
    transport: str,
    target: tuple[str, ...] | str,
    headers: Mapping[str, str],
    timeout: float,
) -> ExecutionResult:
    if transport == "stdio":
        if not isinstance(target, tuple):
            raise AssertionError("stdio target must be an argv tuple")
        return await execute_stdio(actions, target, timeout=timeout)
    if not isinstance(target, str):
        raise AssertionError("HTTP target must be a URL")
    return await execute_http(actions, target, headers=headers, timeout=timeout)


def _spec(protocol_version: str, location: str) -> str:
    return (
        f"https://modelcontextprotocol.io/specification/{protocol_version}/{location}"
    )


def _failure(
    *,
    kind: str,
    spec_reference: str,
    trigger_action_id: str,
    evidence: dict[str, JsonValue],
) -> Failure:
    return Failure(
        kind=kind,
        spec_reference=spec_reference,
        trigger_action_id=trigger_action_id,
        evidence=evidence,
        signature=failure_signature(kind, evidence),
    )


def _response(
    events: Sequence[Mapping[str, object]],
    target_action_id: str,
    *,
    method: str,
    protocol_version: str,
) -> tuple[Mapping[str, object] | None, Failure | None]:
    matches = [
        event
        for event in events
        if event.get("kind") == "response"
        and event.get("target_action_id") == target_action_id
    ]
    if len(matches) == 1:
        return matches[0], None
    reason = "missing" if not matches else "ambiguous"
    evidence: dict[str, JsonValue] = {
        "direction": "server_to_client",
        "method": method,
        "reason": reason,
        "subject": "server",
    }
    return None, _failure(
        kind="protocol.response_missing_or_ambiguous",
        spec_reference=_spec(protocol_version, "basic/index#responses"),
        trigger_action_id=target_action_id,
        evidence=evidence,
    )


def _invalid_result(
    method: str,
    reason: str,
    protocol_version: str,
) -> Failure:
    action_id = {
        "initialize": "initialize",
        "ping": "ping",
        "tools/list": "tools-list",
    }[method]
    location = {
        "initialize": "basic/lifecycle#initialization",
        "ping": "basic/utilities/ping",
        "tools/list": "server/tools#listing-tools",
    }[method]
    evidence: dict[str, JsonValue] = {
        "direction": "server_to_client",
        "method": method,
        "reason": reason,
        "subject": "server",
    }
    return _failure(
        kind=f"protocol.invalid_{method.replace('/', '_')}_result",
        spec_reference=_spec(protocol_version, location),
        trigger_action_id=action_id,
        evidence=evidence,
    )


def _evaluate(
    result: ExecutionResult,
    *,
    protocol_version: str,
    transport: str,
) -> tuple[Failure | None, str | None]:
    if transport == "stdio":
        if result.returncode is None:
            return None, "stdio server cleanup did not report a process status"
        if result.returncode != 0:
            return None, f"stdio server exited with status {result.returncode}"
    elif result.returncode is not None:
        return None, "HTTP execution returned an unexpected process status"
    elif result.cleanup.get("client_closed") is not True:
        return None, "HTTP client cleanup was not confirmed"

    for event in result.events:
        if event.get("kind") == "timeout":
            return None, "the server operation timed out"
        if event.get("kind") == "http_error":
            status = event.get("status")
            label = str(status) if type(status) is int else "invalid"
            return None, f"HTTP {label} prevented the check"

    initialize, failure = _response(
        result.events,
        "initialize",
        method="initialize",
        protocol_version=protocol_version,
    )
    if failure is not None:
        return failure, None
    assert initialize is not None
    if initialize.get("outcome") != "success":
        return _invalid_result("initialize", "error_response", protocol_version), None
    initialize_payload = initialize.get("payload")
    if not isinstance(initialize_payload, Mapping):
        return _invalid_result(
            "initialize", "non_object_result", protocol_version
        ), None
    negotiated = initialize_payload.get("protocolVersion")
    if negotiated not in PROTOCOL_VERSIONS:
        return _invalid_result(
            "initialize",
            "unsupported_protocol_version",
            protocol_version,
        ), None
    capabilities = initialize_payload.get("capabilities")
    server_info = initialize_payload.get("serverInfo")
    if not isinstance(capabilities, Mapping):
        return _invalid_result(
            "initialize", "invalid_capabilities", protocol_version
        ), None
    if (
        not isinstance(server_info, Mapping)
        or not isinstance(server_info.get("name"), str)
        or not isinstance(server_info.get("version"), str)
    ):
        return _invalid_result(
            "initialize", "invalid_server_info", protocol_version
        ), None

    ping, failure = _response(
        result.events,
        "ping",
        method="ping",
        protocol_version=protocol_version,
    )
    if failure is not None:
        return failure, None
    assert ping is not None
    if ping.get("outcome") != "success" or not isinstance(ping.get("payload"), Mapping):
        return _invalid_result("ping", "invalid_response", protocol_version), None

    tools_list, failure = _response(
        result.events,
        "tools-list",
        method="tools/list",
        protocol_version=protocol_version,
    )
    if failure is not None:
        return failure, None
    assert tools_list is not None
    if tools_list.get("outcome") != "success":
        if "tools" in capabilities:
            return _invalid_result(
                "tools/list",
                "error_despite_declared_capability",
                protocol_version,
            ), None
        return None, None

    tools_payload = tools_list.get("payload")
    tools = tools_payload.get("tools") if isinstance(tools_payload, Mapping) else None
    if not isinstance(tools, list):
        return _invalid_result(
            "tools/list", "invalid_tools_array", protocol_version
        ), None
    for tool in tools:
        if (
            not isinstance(tool, Mapping)
            or not isinstance(tool.get("name"), str)
            or not tool.get("name")
            or not isinstance(tool.get("inputSchema"), Mapping)
        ):
            return _invalid_result(
                "tools/list",
                "invalid_tool_definition",
                protocol_version,
            ), None
        if "outputSchema" in tool and not isinstance(tool["outputSchema"], Mapping):
            return _invalid_result(
                "tools/list",
                "invalid_output_schema",
                protocol_version,
            ), None
    if (
        isinstance(tools_payload, Mapping)
        and "nextCursor" in tools_payload
        and not isinstance(tools_payload["nextCursor"], str)
    ):
        return _invalid_result(
            "tools/list", "invalid_next_cursor", protocol_version
        ), None
    return None, None


def _safe_message(
    value: object,
    *,
    secret_values: Sequence[str] = (),
) -> str:
    safe = redact(
        str(value),
        secret_values=secret_values,
        environment=os.environ,
    )
    return safe if isinstance(safe, str) else "operation failed"


def _protocol_exception_failure(
    exc: BaseException,
    *,
    protocol_version: str,
    transport: str,
) -> Failure:
    if isinstance(exc, StdioProtocolError):
        kind = "transport.stdio_invalid_message"
    elif isinstance(exc, HTTPProtocolError):
        kind = "transport.http_invalid_response"
    else:
        kind = "protocol.invalid_peer_message"
    evidence: dict[str, JsonValue] = {
        "direction": "server_to_client",
        "subject": "server",
        "transport": transport,
        "violation": "invalid_message",
    }
    return _failure(
        kind=kind,
        spec_reference=_spec(protocol_version, "basic/index#messages"),
        trigger_action_id="check",
        evidence=evidence,
    )


def _reproducer(
    actions: Sequence[Action],
    trigger_action_id: str,
) -> tuple[dict[str, JsonValue], ...]:
    if not any(action.action_id == trigger_action_id for action in actions):
        return tuple(action.to_dict() for action in actions)
    end = next(
        index
        for index, action in enumerate(actions)
        if action.action_id == trigger_action_id
    )
    for index, action in enumerate(actions[end + 1 :], start=end + 1):
        if (
            action.kind is ActionKind.RESPONSE
            and action.target_action_id == trigger_action_id
        ):
            end = index
            break
    return tuple(action.to_dict() for action in actions[: end + 1])


def _write_check_artifact(
    args: argparse.Namespace,
    *,
    actions: Sequence[Action],
    events: Sequence[Mapping[str, object]],
    transport: str,
    secret_values: Sequence[str],
    outcome: str,
    cleanup: Mapping[str, object] | None = None,
    failure: Failure | None = None,
) -> Path:
    recorder = TraceRecorder(
        protocol_version=args.protocol_version,
        adapter="wire",
        sdk_version="none",
        transport=transport,
        seed=0,
        secret_values=secret_values,
        environment=os.environ,
        fixture_id="server-smoke",
        cleanup=cleanup,
        generation={
            "engine": "mcp-statecheck CLI",
            "outcome": outcome,
            "profile": "quick",
        },
    )
    for action in actions:
        recorder.record_action(action.to_dict())
    for event in events:
        recorder.record_event(event)
    if failure is not None:
        recorder.set_failure(
            kind=failure.kind,
            spec_reference=failure.spec_reference,
            signature=failure.signature,
            minimized_reproducer=_reproducer(
                actions,
                failure.trigger_action_id,
            ),
            trigger_action_id=failure.trigger_action_id,
            evidence=failure.evidence,
        )
    artifact_path = recorder.write(args.output)
    write_reports(
        recorder.artifact(),
        source_path=artifact_path,
        junit_path=args.junit,
        sarif_path=args.sarif,
        html_path=args.html,
        secret_values=secret_values,
        environment=os.environ,
    )
    return artifact_path


def _run_check(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    try:
        transport, target, headers, secrets = _validated_target(args)
        validate_output_paths(
            source_path=args.output,
            junit_path=args.junit,
            sarif_path=args.sarif,
            html_path=args.html,
        )
    except ValueError as exc:
        parser.error(str(exc))
    actions = _smoke_actions(args.protocol_version)

    try:
        result = anyio.run(
            _execute_check,
            actions,
            transport,
            target,
            headers,
            args.timeout,
        )
    except (ExecutionProtocolError, StdioProtocolError, HTTPProtocolError) as exc:
        message = _safe_message(exc, secret_values=secrets)
        failure = _protocol_exception_failure(
            exc,
            protocol_version=args.protocol_version,
            transport=transport,
        )
        events = (
            {
                "kind": "protocol_error",
                "message": message,
                "target_action_id": None,
            },
        )
        try:
            path = _write_check_artifact(
                args,
                actions=actions,
                events=events,
                transport=transport,
                secret_values=secrets,
                outcome="failure",
                failure=failure,
            )
        except (OSError, ReportError) as write_error:
            print(f"mcp-statecheck: {_safe_message(write_error)}", file=sys.stderr)
            return 2
        print(
            f"Check failed: {failure.kind} ({failure.signature}); wrote {path}",
            file=sys.stderr,
        )
        return 1
    except (
        StdioTimeout,
        HTTPTimeout,
        TimeoutError,
        StdioError,
        HTTPTransportError,
    ) as exc:
        message = _safe_message(exc, secret_values=secrets)
        events = (
            {
                "kind": "infrastructure_error",
                "message": message,
                "target_action_id": None,
            },
        )
        try:
            path = _write_check_artifact(
                args,
                actions=actions,
                events=events,
                transport=transport,
                secret_values=secrets,
                outcome="infrastructure_error",
            )
        except (OSError, ReportError) as write_error:
            print(f"mcp-statecheck: {_safe_message(write_error)}", file=sys.stderr)
            return 2
        print(
            f"Check infrastructure error: {message}; wrote {path}",
            file=sys.stderr,
        )
        return 2

    failure, infrastructure_error = _evaluate(
        result,
        protocol_version=args.protocol_version,
        transport=transport,
    )
    events: list[Mapping[str, object]] = list(result.events)
    if infrastructure_error is not None:
        events.append(
            {
                "kind": "infrastructure_error",
                "message": infrastructure_error,
                "target_action_id": None,
            }
        )
    cleanup: dict[str, object] = dict(result.cleanup)
    if transport == "stdio":
        cleanup.update(
            {
                "server_reaped": result.returncode is not None,
                "server_returncode": result.returncode,
            }
        )
    outcome = (
        "infrastructure_error"
        if infrastructure_error is not None
        else "failure"
        if failure is not None
        else "passed"
    )
    try:
        path = _write_check_artifact(
            args,
            actions=actions,
            events=events,
            transport=transport,
            secret_values=secrets,
            outcome=outcome,
            cleanup=cleanup,
            failure=failure,
        )
    except (OSError, ReportError) as exc:
        print(f"mcp-statecheck: {_safe_message(exc)}", file=sys.stderr)
        return 2
    if infrastructure_error is not None:
        print(
            f"Check infrastructure error: {infrastructure_error}; wrote {path}",
            file=sys.stderr,
        )
        return 2
    if failure is not None:
        print(
            f"Check failed: {failure.kind} ({failure.signature}); wrote {path}",
            file=sys.stderr,
        )
        return 1
    print(f"Check passed: wrote {path}")
    return 0


def _run_report(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    if (
        args.json is None
        and args.junit is None
        and args.sarif is None
        and args.html is None
    ):
        parser.error(
            "report requires at least one of --json, --junit, --sarif, or --html"
        )
    try:
        artifact = load_artifact(args.artifact, environment=os.environ)
        written = write_reports(
            artifact,
            source_path=args.artifact,
            json_path=args.json,
            junit_path=args.junit,
            sarif_path=args.sarif,
            html_path=args.html,
            environment=os.environ,
        )
    except ReportError as exc:
        print(f"mcp-statecheck: {_safe_message(exc)}", file=sys.stderr)
        return 2
    print(
        f"Report wrote {len(written)} file{'s' if len(written) != 1 else ''}: "
        + ", ".join(str(path) for path in written)
    )
    return {
        "passed": 0,
        "failure": 1,
        "infrastructure_error": 2,
    }[artifact_status(artifact)]


def _run_matrix(args: argparse.Namespace) -> int:
    from .matrix import (
        MatrixError,
        MatrixFailure,
        check_matrix,
        run_matrix,
    )

    try:
        if args.check:
            check_matrix(args.config, args.output)
        else:
            written = run_matrix(args.config, args.output)
    except MatrixFailure as exc:
        print(
            f"Matrix found a compatibility failure: {_safe_message(exc)}",
            file=sys.stderr,
        )
        return 1
    except MatrixError as exc:
        print(f"mcp-statecheck: {_safe_message(exc)}", file=sys.stderr)
        return 2
    if args.check:
        print("Matrix passed: 16/16 locked SDK transport cells match artifacts")
    else:
        print(f"Matrix passed: wrote {len(written)} locked SDK transport traces")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.subcommand == "check":
        return _run_check(args, parser)
    if args.subcommand == "report":
        return _run_report(args, parser)
    if args.subcommand == "matrix":
        return _run_matrix(args)
    raise AssertionError(f"unhandled subcommand: {args.subcommand}")


def action_main() -> int:
    """Validate the composite Action boundary before dispatching the CLI."""

    raw = os.environ.get("MCP_STATECHECK_ARGUMENTS")
    try:
        arguments = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        print("arguments must be valid JSON", file=sys.stderr)
        return 2
    if not isinstance(arguments, list) or not all(
        isinstance(argument, str) for argument in arguments
    ):
        print("arguments must be a JSON array of strings", file=sys.stderr)
        return 2
    return main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
