# MCP landscape snapshot, refreshed 2026-07-28

This snapshot records the inputs used for the first implementation baseline.
Registry and release data were checked again at 2026-07-28 10:00
Asia/Shanghai.

## Protocol

The latest released MCP revision is
[`2025-11-25`](https://modelcontextprotocol.io/specification/2025-11-25).
At verification time, [`2026-07-28` remained an official release
candidate](https://github.com/modelcontextprotocol/modelcontextprotocol/releases/tag/2026-07-28-RC):
the final specification URL returned 404 and the latest non-prerelease
specification release was still `2025-11-25`. The first matrix therefore
retains released revisions `2025-06-18` and `2025-11-25`. The new revision
removes the legacy initialize/session lifecycle, so it will require a separate
modern action profile rather than a third string-only legacy cell after GA.

The implementation baseline comes from the official
[lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle),
[transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
and [cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)
pages. In particular, Streamable HTTP may return JSON or SSE, session expiry is
an HTTP 404 followed by a fresh initialization, and SSE resumption uses event
IDs with `Last-Event-ID`.

## SDK pins for M3

The live GitHub release API and package registries returned:

| Runner | Stable line | v2 line | Status |
| --- | --- | --- | --- |
| Python | `mcp==1.28.1` | `mcp==2.0.0rc1` with `mcp-types==2.0.0rc1` | v2 prerelease |
| TypeScript | `@modelcontextprotocol/sdk@1.30.0` | `@modelcontextprotocol/client@2.0.0` | v2 stable |

Python v2 remains an exact prerelease pin. TypeScript v2 reached stable before
the final specification URL appeared; it uses split client and server packages
rather than a drop-in version of the v1 SDK package. The client matrix pins
only `@modelcontextprotocol/client`; the server package is not needed.

Sources: [Python SDK releases](https://github.com/modelcontextprotocol/python-sdk/releases),
[Python package](https://pypi.org/project/mcp/),
[TypeScript SDK releases](https://github.com/modelcontextprotocol/typescript-sdk/releases),
[the v1 npm package](https://www.npmjs.com/package/@modelcontextprotocol/sdk),
[the v2 npm package](https://www.npmjs.com/package/@modelcontextprotocol/client),
and [specification releases](https://github.com/modelcontextprotocol/modelcontextprotocol/releases).

## Adjacent tools

The official [conformance framework](https://github.com/modelcontextprotocol/conformance)
is at stable tag `v0.1.16`. It runs fixed client and server scenarios and
supports expected-failure baselines. It does not claim stateful generation,
cross-implementation differential normalization, shrinking, or deterministic
replay. `mcp-statecheck` should consume or complement those scenarios where
useful instead of copying them.

[`mcp-diff`](https://pypi.org/project/mcp-diff/) `0.1.0` is a stdio schema
lockfile checker. Its overlap is limited to subprocess startup, initialization,
`tools/list`, timeouts, and process exit. Schema change classification is not
part of `mcp-statecheck` v0.1.
