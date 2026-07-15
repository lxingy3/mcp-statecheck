# mcp-statecheck

Stateful differential testing for Model Context Protocol implementations.

[![CI](https://github.com/lxingy3/mcp-statecheck/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/lxingy3/mcp-statecheck/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Many MCP failures only appear across a sequence: initialize, overlap requests,
cancel one, reconnect an SSE stream, or recover from an expired session.
`mcp-statecheck` models these interactions as canonical actions and records the
wire behavior as deterministic, redacted traces.

> [!NOTE]
> This project is pre-alpha and remains private. M0 and M1 are complete.
> Generation, differential comparison, shrinking, replay, SDK runners, reports,
> and the CLI are scheduled for M2 through M4.

## What works today

| Area | M1 implementation |
| --- | --- |
| Protocol state | Lifecycle, capability negotiation, pending requests, cancellation, streams, and logical sessions |
| Request identity | Internal action IDs remain separate from MCP request IDs, including duplicate concurrent IDs |
| stdio | JSON Lines subprocess transport with deadlines, bounded stderr, and child cleanup |
| Streamable HTTP | Session headers, JSON and SSE responses, reconnect cursors, status handling, and cleanup |
| Traces | Versioned JSON, deterministic writes, recursive secret redaction, and stable session aliases |
| Controlled peers | Five controlled scenarios exercised over real stdio or localhost HTTP connections |

```mermaid
flowchart LR
    A["Canonical actions"] --> B["State reducer"]
    A --> C["stdio or Streamable HTTP"]
    C --> D["Controlled MCP peer"]
    D --> E["Wire observations"]
    B --> F["Versioned trace"]
    E --> F
    F -. "M2" .-> G["Compare, shrink, replay"]
```

The checked-in [M1 acceptance report](artifacts/m1/acceptance.json) records
Python 3.12.13, 80 passing tests, and all five fixture checks within a
60-second hard deadline. CI runs the same locked project on Ubuntu and Windows.

## Reproduce the M1 baseline

Install [uv](https://docs.astral.sh/uv/), then run:

```console
uv sync --locked
uv run python scripts/run_m1_acceptance.py
uv build
```

The acceptance command runs the full test suite and writes one wire trace per
controlled fixture to `artifacts/m1/`.

A successful report currently includes:

```json
{
  "fixture_checks_passed": 5,
  "milestone": "M1",
  "python": "3.12.13",
  "status": "passed",
  "tests_passed": 80
}
```

## Controlled defect corpus

| Fixture | Transport | Observation preserved by M1 |
| --- | --- | --- |
| `http-error-as-timeout` | Streamable HTTP | An HTTP 503 remains distinct from a timeout |
| `duplicate-concurrent-request-id` | stdio | Two pending calls retain separate logical identities despite sharing one MCP request ID |
| `second-sse-resume-token-loss` | Streamable HTTP | Each reconnect sends the newest event ID |
| `request-before-initialized` | stdio | A request issued before initialization completes remains visible in the trace |
| `late-response-after-cancellation` | stdio | A late cancelled response does not consume the next call |

For example, the HTTP error fixture records the protocol result and cleanup
state separately:

```json
{
  "fixture_id": "http-error-as-timeout",
  "transport": "streamable-http",
  "normalized_events": [
    {
      "kind": "http_error",
      "status": 503
    }
  ],
  "cleanup": {
    "client_closed": true,
    "listener_closed": true
  }
}
```

M1 preserves the observations required for later detection. It does not yet
shrink or replay failures. Those checks belong to M2.

## Project boundaries

The official
[MCP conformance framework](https://github.com/modelcontextprotocol/conformance)
remains the source for fixed specification scenarios. `mcp-statecheck` is
being built for generated stateful sequences, normalized cross-implementation
comparison, shrinking, and deterministic replay. It complements conformance
rather than replacing it.

Schema lockfiles, API compatibility checks, and protocols other than MCP are
outside the v0.1 scope.

## Roadmap

| Milestone | Status | Scope |
| --- | --- | --- |
| M0 | Complete | Reproducible Python 3.12 environment, lockfile, design baseline, and cross-platform CI |
| M1 | Complete | Canonical model, wire transports, trace recorder, and controlled peers |
| M2 | Next | Hypothesis state machine, invariants, differential oracle, signatures, shrinking, and replay |
| M3 | Planned | Isolated Python and TypeScript v1/v2 SDK runners |
| M4 | Planned | CLI, JUnit/SARIF/HTML reports, GitHub Action, documentation, and the v0.1 gate |

The repository will not be made public or released to PyPI until the full
[v0.1 publication gate](docs/design.md#v01-publication-gate) passes.

## Documentation

- [Design baseline](docs/design.md)
- [MCP landscape snapshot](docs/research/2026-07-15-landscape.md)
- [M1 acceptance report](artifacts/m1/acceptance.json)

## License

Apache-2.0.
