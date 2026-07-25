"""Isolated Python SDK client runner for the M3 stdio matrix."""

from __future__ import annotations

import platform
import sys
import traceback
from collections.abc import Mapping
from importlib.metadata import version

import anyio

from mcp_statecheck.adapters.jsonl import Envelope, dumps_line, loads_line
from mcp_statecheck.model import Action, ActionKind

EXPECTED_ACTIONS = (
    (ActionKind.CONNECT, None),
    (ActionKind.INITIALIZE, None),
    (ActionKind.INITIALIZED, None),
    (ActionKind.REQUEST, "ping"),
    (ActionKind.REQUEST, "tools/list"),
    (ActionKind.REQUEST, "tools/call"),
    (ActionKind.CLOSE, None),
)


def _attribute(value: object, *names: str) -> object:
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    raise AttributeError(f"{type(value).__name__} has none of: {', '.join(names)}")


def _command(
    envelope: Envelope,
) -> tuple[str, str, tuple[str, ...], tuple[Action, ...]]:
    if envelope.kind != "run":
        raise ValueError("adapter envelope kind must be 'run'")
    if set(envelope.payload) != {
        "actions",
        "peer_command",
        "runner_id",
        "sdk_version",
    }:
        raise ValueError("adapter payload fields do not match schema v1")

    runner_id = envelope.payload["runner_id"]
    sdk_version = envelope.payload["sdk_version"]
    peer_command = envelope.payload["peer_command"]
    raw_actions = envelope.payload["actions"]
    if runner_id not in {"python-v1", "python-v2"}:
        raise ValueError("unsupported Python runner")
    if not isinstance(sdk_version, str) or not sdk_version:
        raise TypeError("sdk_version must be a non-empty string")
    if (
        not isinstance(peer_command, list)
        or not peer_command
        or not all(isinstance(part, str) and part for part in peer_command)
    ):
        raise TypeError("peer_command must be a non-empty string array")
    if not isinstance(raw_actions, list) or not all(
        isinstance(action, Mapping) for action in raw_actions
    ):
        raise TypeError("actions must be an array of objects")

    actions = tuple(Action.from_dict(action) for action in raw_actions)
    observed = tuple((action.kind, action.method) for action in actions)
    if observed != EXPECTED_ACTIONS:
        raise ValueError("unsupported canonical action sequence")
    if (
        actions[1].protocol_version != "2025-11-25"
        or actions[1].capabilities != {}
        or actions[3].payload != {}
        or actions[4].payload != {}
        or actions[5].payload != {"name": "echo", "arguments": {"text": "hello"}}
    ):
        raise ValueError("unsupported canonical action payload")
    return runner_id, sdk_version, tuple(peer_command), actions


async def _run(peer_command: tuple[str, ...], actions: tuple[Action, ...]):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=peer_command[0],
        args=list(peer_command[1:]),
    )
    events: list[dict[str, object]] = []
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            server_info = _attribute(initialized, "serverInfo", "server_info")
            events.append(
                {
                    "kind": "response",
                    "method": "initialize",
                    "protocol_version": str(
                        _attribute(
                            initialized,
                            "protocolVersion",
                            "protocol_version",
                        )
                    ),
                    "server_info": {
                        "name": _attribute(server_info, "name"),
                        "version": _attribute(server_info, "version"),
                    },
                    "target_action_id": actions[1].action_id,
                }
            )

            await session.send_ping()
            events.append(
                {
                    "kind": "response",
                    "method": "ping",
                    "target_action_id": actions[3].action_id,
                }
            )

            tools = await session.list_tools()
            events.append(
                {
                    "kind": "response",
                    "method": "tools/list",
                    "target_action_id": actions[4].action_id,
                    "tool_names": sorted(tool.name for tool in tools.tools),
                }
            )

            call = actions[5].payload
            if not isinstance(call, dict):
                raise TypeError("tools/call payload must be an object")
            arguments = call.get("arguments")
            if not isinstance(arguments, dict):
                raise TypeError("tools/call arguments must be an object")
            result = await session.call_tool(str(call.get("name")), arguments)
            texts = [
                content.text
                for content in result.content
                if getattr(content, "type", None) == "text"
            ]
            events.append(
                {
                    "is_error": bool(
                        getattr(result, "isError", getattr(result, "is_error", False))
                    ),
                    "kind": "response",
                    "method": "tools/call",
                    "target_action_id": actions[5].action_id,
                    "text": "\n".join(texts),
                }
            )
    return events


def main() -> int:
    try:
        line = sys.stdin.buffer.readline()
        envelope = loads_line(line, line_number=1)
        runner_id, expected_sdk_version, peer_command, actions = _command(envelope)
        actual_sdk_version = version("mcp")
        if actual_sdk_version != expected_sdk_version:
            raise RuntimeError(
                f"loaded mcp {actual_sdk_version}, expected {expected_sdk_version}"
            )
        events = anyio.run(_run, peer_command, actions)
        response = Envelope(
            command_id=envelope.command_id,
            kind="result",
            payload={
                "cleanup": {"client_closed": True},
                "events": events,
                "runner_id": runner_id,
                "runtime_version": platform.python_version(),
                "sdk_version": actual_sdk_version,
            },
        )
        sys.stdout.write(dumps_line(response))
        sys.stdout.flush()
        return 0
    except Exception as exc:
        print(f"python SDK adapter failed: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
