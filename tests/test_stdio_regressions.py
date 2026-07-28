from __future__ import annotations

import gc
import sys
import warnings

import anyio
import pytest

from mcp_statecheck.transports import (
    StdioProtocolError,
    StdioTimeout,
    StdioTransport,
)


def test_stdio_closes_process_and_all_pipe_transports() -> None:
    async def scenario() -> StdioTransport:
        transport = StdioTransport(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            timeout=1,
        )
        async with transport:
            assert transport.pid is not None
        assert transport.returncode == 0
        return transport

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        transport = anyio.run(scenario)
        del transport
        gc.collect()

    assert not [warning for warning in caught if warning.category is ResourceWarning]


def test_stdio_start_timeout_reaps_a_child_created_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_process = anyio.open_process

    async def delayed_open_process(*args: object, **kwargs: object):
        process = await open_process(*args, **kwargs)
        await anyio.sleep(0.05)
        return process

    monkeypatch.setattr(anyio, "open_process", delayed_open_process)

    async def scenario() -> None:
        transport = StdioTransport(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.01,
            shutdown_timeout=0.5,
        )
        with pytest.raises(StdioTimeout, match="starting"):
            await transport.start()
        assert transport.returncode is not None

    anyio.run(scenario)


def test_stdio_rejects_nonfinite_outbound_json() -> None:
    async def scenario() -> None:
        async with StdioTransport(
            [sys.executable, "-c", "import sys; sys.stdin.read()"]
        ) as transport:
            with pytest.raises(StdioProtocolError, match="not JSON serializable"):
                await transport.send({"jsonrpc": "2.0", "value": float("nan")})

    anyio.run(scenario)


@pytest.mark.parametrize(
    "payload",
    [
        '{"jsonrpc":"2.0","id":1,"id":2}',
        '{"jsonrpc":"2.0","id":NaN}',
        r'{"jsonrpc":"2.0","id":1,"result":{"text":"\ud800"}}',
    ],
)
def test_stdio_rejects_ambiguous_inbound_json(payload: str) -> None:
    async def scenario() -> None:
        script = f"print({payload!r}, flush=True)"
        transport = StdioTransport([sys.executable, "-c", script], timeout=1)
        await transport.start()
        with pytest.raises(StdioProtocolError):
            await transport.receive()
        assert transport.returncode is not None

    anyio.run(scenario)
