# M6.3 controlled subscription delivery

The [MCP `2026-07-28` subscriptions specification](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/patterns/subscriptions.mdx)
defines `subscriptions/listen` as a long-lived request. A server must send
`notifications/subscriptions/acknowledged` first for that subscription ID, keep
the ID in each notification's `_meta`, and deliver only requested notification
types. Ordering is per subscription, not global. If the server ends the
subscription itself, it should send a graceful completion response with the
matching ID. The source reviewed for this slice was
`docs/specification/2026-07-28/basic/patterns/subscriptions.mdx` at Git commit
`ab3a39c13bd23be691c2760e1c6c5c15a64582e1` (file blob
`0349e28be419632e1498a3924a8ed0ba2b85d363`).

M6.3 exercises those rules on finite, package-controlled stdio and Streamable
HTTP peers. It reuses the existing wire executors and trace schema. The oracle
reads only normalized messages; it does not read the selected fault mode. A
seed-fixed Hypothesis state machine adds subscription and unrelated tool-list
requests, then shrinks each observed failure. The three faults are delivery
before acknowledgement, a notification with an unknown subscription ID, and
delivery outside the agreed filter. Resource URI filtering and two independent
IDs are covered by conforming baselines.

The acceptance gate regenerates six fault traces twice, requires byte-identical
artifacts, replays each saved trace ten times, and checks eight conforming
cells. The saved recipe names only an allowlisted peer fixture, so an artifact
cannot specify a command or URL. This slice does not exercise SDK subscription
APIs, cancellation, an unbounded stream, or a server supplied by a user. Those
need separate evidence before claiming broader subscription conformance.

## M6.4 cancellation and reconnect follow-up

The [stdio transport rule](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/transports/stdio.mdx)
uses `notifications/cancelled` with the original request ID. The
[Streamable HTTP rule](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/transports/streamable-http.mdx)
treats closing the request's SSE stream as cancellation. In both cases, the
server must not send further messages for the cancelled request. The
subscription specification also distinguishes a graceful completion response
from an abrupt transport close; clients may reconnect, and stdio clients must
send a new `subscriptions/listen` request after reconnecting.

M6.4 checks these paths against package-controlled peers. The stdio client
sends cancellation for request 1, then opens request 2 on the same process. A
mutated peer emits a notification under the cancelled ID after processing the
cancellation; the independent lifecycle check identifies it and replays the
same failure ten times. On HTTP, the client closes an open SSE request, the
peer observes that socket closure, and a new request succeeds. Separate peers
drop the first stream without completion, then accept a fresh subscription.
The gate records five cells from two byte-identical runs. It does not claim
SDK-native handling, arbitrary-target recovery, or all cancellation races.
