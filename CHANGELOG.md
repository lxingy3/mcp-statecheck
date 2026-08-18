# Changelog

## Unreleased

- Added a locked external MCP server canary with ten byte-identical stdio runs,
  direct process cleanup evidence, isolated runtime state, and Ubuntu/Windows
  CI coverage.
- Added pinned Filesystem and Git application-server acceptance with real
  state mutations, tested sibling-mutation rejection, ten-run deterministic
  evidence, isolated credentials, and Linux/Windows/macOS automation.

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
