// Isolated TypeScript SDK client runner for the M3 transport matrix.

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { createInterface } from "node:readline";

const expectedActions = [
  ["connect", null],
  ["initialize", null],
  ["initialized", null],
  ["request", "ping"],
  ["request", "tools/list"],
  ["request", "tools/call"],
  ["close", null],
];
const actionFields =
  "action_id,capabilities,kind,mcp_request_id,method,payload,protocol_version,resume_token,stream_id,target_action_id";
let activeCell;

function fail(condition, message) {
  if (!condition) throw new Error(message);
}

function parseCommand(line) {
  const envelope = JSON.parse(line);
  fail(envelope && typeof envelope === "object" && !Array.isArray(envelope), "envelope must be an object");
  fail(
    Object.keys(envelope).sort().join(",") === "command_id,kind,payload,schema_version",
    "envelope fields do not match schema v1",
  );
  fail(envelope.schema_version === 1, "unsupported schema version");
  fail(typeof envelope.command_id === "string" && envelope.command_id, "invalid command_id");
  fail(envelope.kind === "run", "adapter envelope kind must be 'run'");

  const payload = envelope.payload;
  fail(payload && typeof payload === "object" && !Array.isArray(payload), "payload must be an object");
  fail(
    Object.keys(payload).sort().join(",") === "actions,runner_id,sdk_version,target,transport",
    "adapter payload fields do not match schema v1",
  );
  fail(["typescript-v1", "typescript-v2"].includes(payload.runner_id), "unsupported TypeScript runner");
  fail(typeof payload.sdk_version === "string" && payload.sdk_version, "invalid sdk_version");
  fail(["stdio", "streamable-http"].includes(payload.transport), "unsupported transport");
  if (payload.transport === "stdio") {
    fail(
      Array.isArray(payload.target) &&
        payload.target.length > 0 &&
        payload.target.every((part) => typeof part === "string" && part),
      "stdio target must be a non-empty string array",
    );
  } else {
    fail(typeof payload.target === "string", "Streamable HTTP target must be a string");
    const target = new URL(payload.target);
    fail(
      target.protocol === "http:" &&
        target.hostname === "127.0.0.1" &&
        target.pathname === "/mcp" &&
        target.username === "" &&
        target.password === "" &&
        target.search === "" &&
        target.hash === "",
      "Streamable HTTP target must be a loopback /mcp URL",
    );
  }
  fail(Array.isArray(payload.actions) && payload.actions.length === expectedActions.length, "unsupported action count");
  payload.actions.forEach((action, index) => {
    fail(action && typeof action === "object" && !Array.isArray(action), "action must be an object");
    fail(Object.keys(action).sort().join(",") === actionFields, "action fields do not match schema v1");
    fail(typeof action.action_id === "string" && action.action_id, "invalid action_id");
    for (const field of ["method", "protocol_version", "resume_token", "stream_id", "target_action_id"]) {
      fail(action[field] === null || typeof action[field] === "string", `invalid action ${field}`);
    }
    fail(
      action.kind === expectedActions[index][0] && action.method === expectedActions[index][1],
      "unsupported canonical action sequence",
    );
  });
  fail(
    payload.actions[1].protocol_version === "2025-11-25" &&
      Object.keys(payload.actions[1].capabilities).length === 0 &&
      Object.keys(payload.actions[3].payload).length === 0 &&
      Object.keys(payload.actions[4].payload).length === 0 &&
      payload.actions[5].payload?.name === "echo" &&
      Object.keys(payload.actions[5].payload).sort().join(",") === "arguments,name" &&
      payload.actions[5].payload?.arguments?.text === "hello" &&
      Object.keys(payload.actions[5].payload.arguments).join(",") === "text",
    "unsupported canonical action payload",
  );
  return { envelope, payload };
}

async function readCommand() {
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of input) {
    input.close();
    return parseCommand(line);
  }
  throw new Error("adapter stdin closed before a command");
}

async function main() {
  const { envelope, payload } = await readCommand();
  const environmentRoot = process.env.MCP_STATECHECK_NODE_ENV;
  fail(environmentRoot, "MCP_STATECHECK_NODE_ENV is required");

  const requireFromEnvironment = createRequire(join(environmentRoot, "package.json"));
  const isV1 = payload.runner_id === "typescript-v1";
  const packageName = isV1 ? "@modelcontextprotocol/sdk" : "@modelcontextprotocol/client";
  const packagePath = join(environmentRoot, "node_modules", ...packageName.split("/"), "package.json");
  const actualSdkVersion = JSON.parse(readFileSync(packagePath, "utf8")).version;
  fail(actualSdkVersion === payload.sdk_version, `loaded ${packageName} ${actualSdkVersion}, expected ${payload.sdk_version}`);
  activeCell = {
    command_id: envelope.command_id,
    runner_id: payload.runner_id,
  };

  const clientModule = requireFromEnvironment(
    isV1 ? "@modelcontextprotocol/sdk/client/index.js" : "@modelcontextprotocol/client",
  );
  const { Client } = clientModule;
  const client = new Client(
    { name: "mcp-statecheck", version: "0.1.0" },
    isV1
      ? { capabilities: {} }
      : {
          capabilities: {},
          versionNegotiation: { mode: "legacy" },
        },
  );
  let transport;
  if (payload.transport === "stdio") {
    const { StdioClientTransport } = requireFromEnvironment(
      isV1 ? "@modelcontextprotocol/sdk/client/stdio.js" : "@modelcontextprotocol/client/stdio",
    );
    transport = new StdioClientTransport({
      command: payload.target[0],
      args: payload.target.slice(1),
    });
  } else {
    const { StreamableHTTPClientTransport } = isV1
      ? requireFromEnvironment("@modelcontextprotocol/sdk/client/streamableHttp.js")
      : clientModule;
    transport = new StreamableHTTPClientTransport(new URL(payload.target));
  }
  let negotiatedProtocolVersion;
  const setProtocolVersion = transport.setProtocolVersion?.bind(transport);
  transport.setProtocolVersion = (protocolVersion) => {
    negotiatedProtocolVersion = protocolVersion;
    setProtocolVersion?.(protocolVersion);
  };

  const events = [];
  try {
    await client.connect(transport);
    const serverInfo = client.getServerVersion();
    events.push({
      kind: "response",
      method: "initialize",
      protocol_version: negotiatedProtocolVersion,
      server_info: { name: serverInfo?.name, version: serverInfo?.version },
      target_action_id: payload.actions[1].action_id,
    });

    await client.ping();
    events.push({
      kind: "response",
      method: "ping",
      target_action_id: payload.actions[3].action_id,
    });

    const tools = await client.listTools();
    events.push({
      kind: "response",
      method: "tools/list",
      target_action_id: payload.actions[4].action_id,
      tool_names: tools.tools.map((tool) => tool.name).sort(),
    });

    const call = payload.actions[5].payload;
    fail(call && typeof call === "object" && !Array.isArray(call), "tools/call payload must be an object");
    const result = await client.callTool({ name: call.name, arguments: call.arguments });
    events.push({
      is_error: Boolean(result.isError),
      kind: "response",
      method: "tools/call",
      target_action_id: payload.actions[5].action_id,
      text: result.content
        .filter((content) => content.type === "text")
        .map((content) => content.text)
        .join("\n"),
    });
  } finally {
    try {
      if (payload.transport === "streamable-http") await transport.terminateSession();
    } finally {
      await client.close();
    }
  }

  const response = {
    command_id: envelope.command_id,
    kind: "result",
    payload: {
      cleanup: { client_closed: true },
      events,
      runner_id: payload.runner_id,
      runtime_version: process.versions.node,
      sdk_version: actualSdkVersion,
    },
    schema_version: 1,
  };
  process.stdout.write(`${JSON.stringify(response)}\n`);
}

main().catch((error) => {
  const errorType = error instanceof Error ? error.constructor.name : typeof error;
  const message =
    (error instanceof Error ? error.message : String(error)) || errorType;
  if (activeCell) {
    process.stdout.write(`${JSON.stringify({
      command_id: activeCell.command_id,
      kind: "failure",
      payload: {
        error_type: errorType,
        message,
        runner_id: activeCell.runner_id,
      },
      schema_version: 1,
    })}\n`);
    process.stderr.write(`TypeScript SDK cell failed: ${message}\n`);
    return;
  }
  process.stderr.write(`TypeScript SDK adapter setup failed: ${message}\n`);
  process.exitCode = 2;
});
