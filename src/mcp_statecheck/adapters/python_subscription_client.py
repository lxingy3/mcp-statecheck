"""Pinned Python SDK subscription probe, run in its isolated environment."""

from __future__ import annotations

import json
import platform
import sys
from importlib.metadata import version
from urllib.parse import urlsplit

import anyio
from mcp import Client, StdioServerParameters
from mcp.client.subscriptions import SubscriptionLost
from mcp.types import Implementation

PROTOCOL = "2026-07-28"


async def _listen(client: Client, *, cancel: bool = False) -> dict:
    events = []
    termination = "graceful"
    async with client.listen(tools_list_changed=True) as subscription:
        honored = subscription.honored.tools_list_changed
        try:
            async for event in subscription:
                events.append(type(event).__name__)
                if cancel:
                    termination = "local"
                    break
        except SubscriptionLost:
            termination = "lost"
    return {
        "events": events,
        "honored_filter": {"toolsListChanged": honored},
        "termination": termination,
    }


async def _probe(transport: str, target: list[str] | str, scenario: str) -> dict:
    if transport == "stdio":
        if (
            not isinstance(target, list)
            or not target
            or not all(isinstance(part, str) and part for part in target)
        ):
            raise ValueError("stdio target must be an argv list")
        server = StdioServerParameters(command=target[0], args=target[1:])
    elif transport == "streamable-http":
        if not isinstance(target, str):
            raise ValueError("HTTP target must be a URL")
        url = urlsplit(target)
        if (url.scheme, url.hostname, url.path) != (
            "http",
            "127.0.0.1",
            "/mcp",
        ) or any((url.username, url.password, url.query, url.fragment)):
            raise ValueError("HTTP target must be loopback /mcp")
        server = target
    else:
        raise ValueError("unsupported subscription transport")

    client = Client(
        server,
        client_info=Implementation(name="mcp-statecheck", version="0.1.0"),
        mode=PROTOCOL,
    )
    async with client:
        if client.protocol_version != PROTOCOL:
            raise RuntimeError("Python SDK did not negotiate the modern protocol")
        with anyio.fail_after(10):
            result = await _listen(client, cancel=scenario == "cancel")
            if scenario == "cancel":
                result["replacement"] = await _listen(client)
            if scenario in {"cancel", "refetch"}:
                tools = await client.list_tools(cache_mode="refresh")
                result["refetched_tools"] = [tool.name for tool in tools.tools]
    return {
        **result,
        "client_closed": True,
        "protocol_version": PROTOCOL,
        "runtime_version": platform.python_version(),
        "sdk_version": version("mcp"),
    }


async def run(command: dict) -> dict:
    scenario = command.get("scenario", "graceful")
    keys = {"transport", "target"}
    if "scenario" in command:
        keys.add("scenario")
    if scenario == "drop":
        keys.add("recovery_target")
    if set(command) != keys or scenario not in {"graceful", "cancel", "drop"}:
        raise ValueError("invalid subscription probe command")
    result = await _probe(command["transport"], command["target"], scenario)
    if scenario == "drop":
        result["replacement"] = await _probe(
            command["transport"], command["recovery_target"], "refetch"
        )
    return result


if __name__ == "__main__":
    result = anyio.run(run, json.load(sys.stdin))
    print(json.dumps(result, sort_keys=True))
