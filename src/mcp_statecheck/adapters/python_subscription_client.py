"""Pinned Python SDK subscription probe, run in its isolated environment."""

from __future__ import annotations

import json
import platform
import sys
from importlib.metadata import version
from urllib.parse import urlsplit

import anyio
from mcp import Client, StdioServerParameters
from mcp.types import Implementation

PROTOCOL = "2026-07-28"


async def run(command: dict) -> dict:
    if set(command) != {"transport", "target"}:
        raise ValueError("invalid subscription probe command")
    transport, target = command["transport"], command["target"]
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
        async with client.listen(tools_list_changed=True) as subscription:
            honored = subscription.honored.tools_list_changed
            events = [type(event).__name__ async for event in subscription]
    return {
        "client_closed": True,
        "events": events,
        "honored_filter": {"toolsListChanged": honored},
        "protocol_version": PROTOCOL,
        "runtime_version": platform.python_version(),
        "sdk_version": version("mcp"),
        "termination": "graceful",
    }


if __name__ == "__main__":
    result = anyio.run(run, json.load(sys.stdin))
    print(json.dumps(result, sort_keys=True))
