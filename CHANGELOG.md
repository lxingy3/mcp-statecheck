# Changelog

## Unreleased

- Added a separate MCP `2026-07-28` stateless matrix with pinned Python
  `mcp 2.1.1` and TypeScript `@modelcontextprotocol/client 2.0.0` clients over
  stdio and Streamable HTTP.
- Added discovery, namespaced per-request metadata, HTTP routing-header and
  session-free transport checks, four parent-owned hard-timeout cleanup probes
  with adapter diagnostics, and three-run deterministic acceptance for the
  modern profile.
- Added a locked external MCP server canary with ten byte-identical stdio runs,
  direct process cleanup evidence, isolated runtime state, and Ubuntu/Windows
  CI coverage.
- Added pinned Filesystem and Git application-server acceptance with real
  state mutations, tested sibling-mutation rejection, ten-run deterministic
  evidence, isolated credentials, and Linux/Windows/macOS automation.
- Added strict, release-bound application recipe manifests that cannot encode
  executable inputs and are validated before external tool discovery.
- Extended Filesystem acceptance through write, edit, read, list, and three
  sibling-boundary checks, with exact host-side byte and directory oracles.
- Extended Git acceptance through status, add, staged diff, deterministic
  commit, log, clean-state, branch, and sibling-repository checks.

## 0.1.0 - 2026-07-30

First public release.

- Added stateful MCP modeling for lifecycle, requests, cancellation, sessions,
  streams, and transport failures.
- Added real stdio and Streamable HTTP execution with hard timeouts and cleanup.
- Added five seeded Hypothesis state machines with shrinking, stable failure
  signatures, and ten-run deterministic replay.
- Added a 16-cell Python/TypeScript SDK, protocol, and transport matrix.
- Added the `check`, `replay`, `matrix`, and `report` commands.
- Added deterministic JSON, JUnit XML, SARIF 2.1.0, and offline HTML reports.
- Added clean wheel/sdist acceptance, a composite GitHub Action, cross-platform
  CI, and secret scanning.

See the [v0.1.0 release notes](docs/releases/v0.1.0.md) for the benchmark
snapshot and limitations.
