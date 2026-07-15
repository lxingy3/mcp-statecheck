from __future__ import annotations

import gc
import sys
import warnings

import anyio
import pytest

from mcp_statecheck.transports import StdioProtocolError, StdioTransport


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
