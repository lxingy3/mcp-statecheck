# MCP 2026-07-28 modern profile

Checked on 2026-08-27.

## Why this is a separate profile

MCP `2026-07-28` is a new protocol era rather than another compatible value in
mcp-statecheck's legacy matrix. The specification removes the initialize
handshake and adds the optional
[`server/discover`](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/discover.mdx)
RPC. Client identity, capabilities, and protocol version move into namespaced
per-request metadata, and protocol-level sessions are removed. The
[Streamable HTTP transport](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/transports/streamable-http.mdx)
also removes the standalone GET stream and adds routable protocol, method, and
name headers.

The existing 16-cell matrix intentionally preserves the 2025 sequence:
initialize, initialized, ping, list tools, call a tool, and close. Reusing that
sequence with only a different version string would not exercise the 2026
protocol.

## Locked SDK surface

The modern profile uses the current released v2 clients verified on the check
date:

- Python [`mcp 2.1.1`](https://pypi.org/project/mcp/2.1.1/) under Python
  3.12.13;
- TypeScript
  [`@modelcontextprotocol/client 2.0.0`](https://www.npmjs.com/package/@modelcontextprotocol/client/v/2.0.0)
  under Node.js 24.14.1.

The package version, runtime, manifest, and transitive dependencies are locked
in `benchmarks/mcp-modern.toml` and the committed Python and npm lockfiles. Each
matrix run copies only those allowlisted inputs into a temporary workspace.

## Acceptance sequence

Each runner executes the same five canonical actions over stdio and Streamable
HTTP:

1. connect;
2. discover the server;
3. list tools;
4. call the controlled `echo` tool;
5. close.

The differential oracle requires one normalized response sequence from both
SDKs. The controlled peer independently records request metadata and HTTP
routing headers, rejects session or standalone-stream behavior, and verifies
cleanup. A separate hanging call for every SDK and transport must hit the outer
deadline and still reap its adapter and peer or close its listener.

The four traces live under `artifacts/m6/matrix/`. The acceptance runner creates
the complete matrix three times in fresh temporary workspaces and requires all
bytes to match before comparing them with the checked evidence:

```console
uv run python scripts/run_m6_modern_acceptance.py --check
```

## Deliberate exclusions

M6.1 is a bounded valid-client baseline. It does not cover Tasks, subscriptions,
modern cancellation sequences, custom `x-mcp-header` parameters, arbitrary
targets, or generated `deep` profiles. Cache hints, partial results, and other
result-shape extensions are not claimed as independent acceptance dimensions.
