# Design baseline

## Scope

`mcp-statecheck` generates protocol interaction sequences, executes them
against isolated MCP implementations, normalizes observations, compares
behavior, and reduces failures to deterministic traces.

Version 0.1 supports MCP only. It covers stdio and Streamable HTTP, including
initialization, capability negotiation, concurrent requests, cancellation,
sessions, SSE resumption, and transport errors. Tasks and other agent
protocols are outside this release.

The official MCP conformance framework remains the source for fixed
specification scenarios. This project focuses on stateful generation,
cross-implementation comparison, and reduction. Schema lockfiles and API
compatibility checks are also outside scope.

## Architecture

The core uses five boundaries:

1. A canonical model represents actions, lifecycle state, logical sessions,
   streams, and pending requests. Its internal action IDs never reuse an MCP
   request ID as an identity key.
2. Wire and SDK adapters run in isolated subprocesses. The core and adapters
   exchange versioned JSON Lines envelopes. IPC command IDs are independent
   of MCP request IDs.
3. Transports own wire mechanics only. Stdio owns subprocess pipes and cleanup.
   Streamable HTTP owns HTTP status, session headers, SSE parsing, and cursors.
4. The recorder redacts data before persistence and writes versioned traces.
5. Invariants, differential comparison, shrinking, and replay consume the
   canonical trace without depending on an SDK's public object model.

Lifecycle and transport behavior are keyed by protocol revision. Session state
is not a universal property of the model because future protocol revisions may
change or remove it.

## Error and cleanup rules

HTTP status and timeout are separate observations. A response with status 401,
403, 404, or 5xx is never normalized as a timeout. A session-bound 404 clears
the wire session and emits a session-expired observation; a later initialize is
an explicit canonical action, not a hidden retry.

SSE cursors are tracked per logical stream. Resumption uses GET and
`Last-Event-ID`, including every subsequent disconnect.

Every asynchronous operation has a deadline. Cleanup closes response streams
and clients, closes subprocess input, waits for clean exit, then terminates and
kills only the direct child if needed. Test servers bind to dynamic localhost
ports and prove that their listener closed.

Recorder redaction covers authorization and cookie headers, common secret
field names, explicitly referenced environment values, and values from
secret-like environment keys. It does not blanket-redact every environment
value.

## Milestone boundaries

- M0: repository, Python 3.12 lockfile, design baseline, and Linux/Windows test
  workflow.
- M1: canonical reducer, JSONL envelope, stdio and Streamable HTTP wire
  transports, trace recorder, and five controlled fixtures.
- M2: Hypothesis state machine, normative invariants, differential oracle,
  failure signatures, shrinking, and ten-run deterministic replay.
- M3: isolated Python and TypeScript v1/v2 runners and the real matrix.
- M4: CLI, JUnit/SARIF/HTML output, GitHub Action, user documentation, and the
  v0.1 release gate.

M1 fixtures preserve the observations needed for later detection. They do not
claim the final fixture gate, which requires M2 shrinking and replay.

The local M1 check is:

```console
uv run python scripts/run_m1_acceptance.py
```

It runs the full M1 suite, including all five peers, under an outer hard
deadline. It writes deterministic traces plus `artifacts/m1/acceptance.json`.
A passing M1 report confirms wire behavior, recording, redaction, and cleanup
only.

## M2 implementation status

M2 is being delivered as one complete fixture slice at a time. The first slice,
`request-before-initialized`, is complete:

- a seeded Hypothesis `RuleBasedStateMachine` generates canonical actions and
  executes each failing candidate against a real stdio peer;
- the lifecycle invariant classifies the client's non-ping request while the
  initialize response is still outstanding;
- the failure signature excludes internal action IDs and wire request IDs;
- Hypothesis shrinks the trace to `initialize` followed by `tools/list`;
- the minimized trace is saved, loaded back from disk, and replayed against ten
  fresh peer processes with one signature and clean exits.

The versioned evidence is checked in at
`artifacts/m2/request-before-initialized.json`. This is not the M2 milestone
gate: the other four controlled defects and the differential oracle remain to
be completed.

## v0.1 publication gate

The repository remains private until all of these are true:

- stdio and Streamable HTTP work against real servers;
- generation, shrinking, and replay work;
- all five seeded defects shrink to their expected actions and replay ten times
  with one signature;
- the pinned Python and TypeScript v1/v2 matrix has real results;
- the quickstart works in a clean environment;
- wheel and sdist builds pass;
- secret scanning passes;
- JSON, JUnit, SARIF, and offline HTML reports pass tests;
- no known P0 or P1 defect remains.
