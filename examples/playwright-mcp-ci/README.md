# Playwright MCP tool catalog in CI

This example uses the PyPI release of `mcp-statecheck==0.2.0` to compare
Microsoft's Playwright MCP `0.0.82` and `0.0.83` over stdio. The npm aliases
let both versions install in the same directory. The lockfiles pin their
dependencies and those of the checker.

## Run locally

Use Node.js 24.14.1 with its bundled npm and [uv](https://docs.astral.sh/uv/)
0.11.28. Run these commands from the repository root.

```sh
cd examples/playwright-mcp-ci
npm ci --ignore-scripts --no-audit --no-fund
uv run --locked python verify.py
```

The commands are the same in PowerShell and a POSIX shell. The Python version
pin selects 3.12.13. The check uses a temporary working directory and reads
the exposed tool catalog. It needs no browser binaries, credentials, or
running browser.

## What the job checks

| Comparison | `compare` exit | Expected result |
| --- | --- | --- |
| `0.0.82` against a fresh `0.0.82` process | 0 | No catalog differences |
| `0.0.82` against `0.0.83` | 1 | Changed input schemas for `browser_find` and `browser_wait_for` |

Both versions expose 25 tools in this configuration. The upgrade adds an
optional `filename` parameter to `browser_find` and updates the description of
`browser_wait_for.time` to say the wait is at most 30 seconds. The latter
registers because the description is inside the input schema. These are
expected changes for this pair of releases.
[Playwright MCP v0.0.83 release notes](https://github.com/microsoft/playwright-mcp/releases/tag/v0.0.83)

The example passes when both comparisons produce the expected exit code and
JSON summary. Find the three captures and two comparison reports under
`artifacts/` after a local run. The
[GitHub Actions workflow](../../.github/workflows/playwright-mcp-catalog.yml)
runs the same commands and uploads those files on Ubuntu and Windows.

## Use the check on an upgrade

Capture the current and proposed server versions with `mcp-statecheck check`,
using the same protocol, transport, and server configuration. Keep the
baseline report from a known-good version. Run `compare` directly so that any
catalog change or invalid input fails the CI step. You can try it from this
directory after running the example.

```sh
uv run --locked mcp-statecheck compare artifacts/baseline.json artifacts/candidate.json
```

This command exits 1 for the pinned upgrade. Review the changed schemas in
the captures before replacing a baseline. The check covers the advertised
contract and makes no tool calls.
