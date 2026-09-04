# Tasks extension profile

Checked on 2026-09-04 against protocol revision `2026-07-28`.

## Specification boundary

Tasks belong to the `io.modelcontextprotocol/tasks` extension. The reference is
the released extension specification and schema at ext-tasks commit
`9263312d11a682ac83f83fe84794d4627efd22f5`:

- [Tasks specification](https://github.com/modelcontextprotocol/ext-tasks/blob/9263312d11a682ac83f83fe84794d4627efd22f5/specification/2026-07-28/tasks.md)
- [Released TypeScript schema](https://github.com/modelcontextprotocol/ext-tasks/blob/9263312d11a682ac83f83fe84794d4627efd22f5/schema/2026-07-28/schema.ts)

The extension replaces the experimental 2025 Tasks surface. It uses
`tasks/get`, `tasks/update`, and `tasks/cancel`; `tasks/result` and `tasks/list`
are not part of this version. Task creation is server-directed and currently
applies to `tools/call` only. Each relevant request declares the extension in
`_meta["io.modelcontextprotocol/clientCapabilities"].extensions`.

`CreateTaskResult` is flat: `resultType: "task"` and the task fields occupy the
same object. A `tasks/get` result also has flat task fields, but its
`resultType` is `"complete"`. Completed tasks carry the original tool result;
failed tasks carry a JSON-RPC error. A tool result with `isError: true` still
uses task status `completed`.

The following sections define the profile's observable contracts:

| Contract | Specification section |
| --- | --- |
| Creation is queryable before its handle is returned | Task Creation |
| State-specific result, error, and input fields | Task Status; Task Polling |
| Input keys cannot be reused for different requests | Task Update Requests |
| Update and cancel return empty acknowledgements with `resultType: "complete"` | Task Update Requests; Task Cancellation |
| Unknown `tasks/get` IDs return `-32602` | Error Handling / Protocol Errors |
| Missing per-request extension capability returns `-32021` | Capability Negotiation; Error Handling / Protocol Errors |
| HTTP `Mcp-Name` equals `params.taskId` | Streamable HTTP: Routing Headers |

Cancellation is cooperative. An acknowledged cancellation may leave the task
working, and the task may finish with a status other than `cancelled`. A
correct oracle cannot require an immediate or eventual cancelled state merely
because it observed the acknowledgement. Update acknowledgements are also
eventually consistent; outstanding input may remain visible temporarily.

The Task Status diagram treats `completed`, `failed`, and `cancelled` as
terminal. Task Polling separately permits expiry handling: after a finite TTL
elapses, the server may mark a task failed and subsequently discard it. A
controlled terminal-state regression case therefore uses `ttlMs: null` to
exclude expiry. This case does not establish a universal invariant for
arbitrary servers with finite retention policies.

## Pinned SDK compatibility

The M6.1 SDK versions remain the comparison point:

| Runtime and package | Observed Tasks behavior |
| --- | --- |
| Python 3.12.13, `mcp==2.1.1` | An advertisement alone does not accept a task result. A custom public client extension can claim the task shape and poll with a custom request type. |
| Node.js 24.14.1, `@modelcontextprotocol/client==2.0.0` | `tasks/get` and `tasks/cancel` are rejected before transport send. A task result on `tools/call` is rejected even with an explicit result schema. |

These are API compatibility observations from controlled in-memory probes,
not an upstream defect claim or a transport conformance result. A Tasks wire
profile must be reported separately from the four-cell M6.1 SDK matrix.

### TypeScript public API probe

Run the following as an ES module from the installed, locked TypeScript v2
runner environment. The transport is a small in-memory test double;
`connect({ prior })` is the public API for using an existing discovery result.
No SDK internals are modified.

```javascript
import { Client } from "@modelcontextprotocol/client";
import { z } from "zod";

const extension = "io.modelcontextprotocol/tasks";
const sent = [];
const task = {
  resultType: "task", taskId: "task-probe", status: "working",
  createdAt: "2026-09-04T00:00:00Z",
  lastUpdatedAt: "2026-09-04T00:00:00Z", ttlMs: null,
};
const transport = {
  async start() {},
  async close() { this.onclose?.(); },
  async send(message) {
    sent.push(message.method);
    const result = message.method === "tools/call"
      ? task : { resultType: "complete" };
    queueMicrotask(() => this.onmessage?.({
      jsonrpc: "2.0", id: message.id, result,
    }));
  },
  setProtocolVersion(version) { this.version = version; },
};
const client = new Client(
  { name: "tasks-compatibility-probe", version: "1" },
  { capabilities: { extensions: { [extension]: {} } } },
);
await client.connect(transport, { prior: {
  kind: "modern", discover: {
    supportedVersions: ["2026-07-28"],
    capabilities: { tools: {}, extensions: { [extension]: {} } },
  },
} });
for (const method of ["tasks/get", "tasks/cancel", "tasks/update", "tools/call"]) {
  const before = sent.length;
  const params = method === "tools/call"
    ? { name: "probe", arguments: {} }
    : { taskId: "task-probe",
        ...(method === "tasks/update" ? { inputResponses: {} } : {}) };
  try {
    await client.request({ method, params }, z.looseObject({}));
    console.log(method, "accepted", sent.length - before);
  } catch (error) {
    console.log(method, error.code, sent.length - before);
  }
}
await client.close();
```

Observed output, where the final column counts transport sends:

```text
tasks/get METHOD_NOT_SUPPORTED_BY_PROTOCOL_VERSION 0
tasks/cancel METHOD_NOT_SUPPORTED_BY_PROTOCOL_VERSION 0
tasks/update accepted 1
tools/call UNSUPPORTED_RESULT_TYPE 1
```

The corresponding package source is pinned at TypeScript SDK commit
`cc4b41617ce3601b1290d67216ea0b194a3cd9ac`. Its
[migration guide](https://github.com/modelcontextprotocol/typescript-sdk/blob/cc4b41617ce3601b1290d67216ea0b194a3cd9ac/docs/migration/support-2026-07-28.md#tasks-deprecated-wire-vocabulary)
describes the retained Tasks types as deprecated 2025 wire vocabulary. The
explicit-schema API does not bypass the negotiated-version method gate.

### Python extension entry points

Python SDK `v2.1.1`, commit
`0921d94a74db900dccd2d534842aa7b6160542d2`, documents the Tasks runtime as
[deferred](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/examples/stories/tasks/README.md).
The public extension mechanism is available:

- [`ClientExtension` and `ResultClaim`](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/client/extension.py)
  register a `result_type="task"` claim for `tools/call`.
- [`ClientSession.send_request`](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/client/session.py)
  accepts an extension request subclass and a result model.
- A request subclass with `name_param = "taskId"` supplies the Tasks routing
  header through the SDK's documented
  [extension verb interface](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/docs_src/extensions/tutorial007.py).

A local probe used `Client(mode="2026-07-28")` against an in-memory
`MCPServer` with a controlled extension. The server returned the flat task
handle above and a completed `tasks/get` response. With
`extensions=[advertise("io.modelcontextprotocol/tasks")]`, `call_tool`
rejected the handle with `ValidationError`. With a custom `ClientExtension`
whose `ResultClaim` resolver sent `tasks/get` and validated the nested tool
result, `call_tool` returned `probe complete`.

That probe establishes a usable extension seam. It does not establish a
built-in Tasks client, input handling, cancellation behavior, HTTP routing,
or lifecycle conformance for the Python SDK.

## Controlled fault candidates

Useful deterministic faults include a changed task ID, a completed task
without its final result, a failed task without a JSON-RPC error, reuse of an
input key for a different request, and a return to working after an observed
terminal state with unlimited retention. They should be labeled seeded
fixture faults. None constitutes evidence of a defect in an external server.

Subscription delivery, finite-TTL races, authorization isolation, durable
storage guarantees, and SDK-native Tasks coverage require separate evidence.
