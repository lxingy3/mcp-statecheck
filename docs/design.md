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

M2 is complete across all five controlled fixtures. Each slice:

- a seeded Hypothesis `RuleBasedStateMachine` generates canonical actions and
  executes each failing candidate against a real stdio or localhost HTTP peer;
- the failure signature excludes internal action IDs and wire request IDs;
- the minimized trace is saved, loaded back from disk, and replayed against ten
  fresh peers with one signature and verified cleanup.

`request-before-initialized` shrinks to `initialize` followed by `tools/list`.
`duplicate-concurrent-request-id` shrinks to the four outbound actions defined
by the fixture plus one explicit initialize-response barrier used to preserve
wire order. Its invariant classifies the requestor's ID reuse; reversed,
ambiguous responses remain observations and are not attributed to the server.

`late-response-after-cancellation` shrinks to five outbound actions plus three
explicit response barriers. The [MCP cancellation specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)
allows cancellation and response delivery to race, so a late response alone is
not a failure. Instead, the controlled peer waits until a later call is pending
and cross-swaps per-request fixture canaries. The differential oracle requires
an explicit fixture scope, a cancellation ID matching the source request, and
the full reciprocal swap before classifying the result as a response-correlation
failure. It does not assume an order for the two concurrent responses. The
canaries are controlled-fixture evidence, not a general semantic invariant for
arbitrary MCP servers; the corresponding normative rule is that each
[response carries the ID of its request](https://modelcontextprotocol.io/specification/2025-11-25/basic/index#responses).

`http-error-as-timeout` first observes a real HTTP 503 through the correct
transport path, then applies a controlled client-adapter fault that reports the
same observation as a timeout. The differential oracle requires both the
source status and the faulty classification, so a normal 503 or a real timeout
does not fail.

`second-sse-resume-token-loss` preserves the latest cursor in the canonical
action while the controlled client adapter drops it only on the second
reconnect. A successful initialize and the required initialized notification
precede the stream actions. The peer records the actual `Last-Event-ID` headers
as `[null, "cursor-1", null]`, plus the session and protocol headers. The
[resumability guidance](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#resumability-and-redelivery)
uses a SHOULD requirement, so this remains a fixture-scoped differential
compatibility failure rather than a universal conformance claim.

The five versioned failures are checked in under `artifacts/m2/`. This closes
the controlled-fixture M2 gate. Real cross-SDK differential execution remains
the M3 milestone.

## v0.1 release gate

The source repository may be public during development. No v0.1 GitHub Release
or PyPI publication happens until all of these are true:

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
