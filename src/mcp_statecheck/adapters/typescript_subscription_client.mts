// Pinned TypeScript SDK subscription probe, run in its isolated environment.

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

const protocol = "2026-07-28";
const environment = process.env.MCP_STATECHECK_NODE_ENV;
if (!environment) throw new Error("MCP_STATECHECK_NODE_ENV is required");
const requireSdk = createRequire(join(environment, "package.json"));
const { Client, StreamableHTTPClientTransport } = requireSdk("@modelcontextprotocol/client");
const { StdioClientTransport } = requireSdk("@modelcontextprotocol/client/stdio");
const sdkVersion = JSON.parse(
  readFileSync(join(environment, "node_modules", "@modelcontextprotocol", "client", "package.json"), "utf8"),
).version;

let input = "";
for await (const chunk of process.stdin) input += chunk;
const command = JSON.parse(input);
if (Object.keys(command).sort().join(",") !== "target,transport") {
  throw new Error("invalid subscription probe command");
}

let transport;
if (command.transport === "stdio") {
  if (!Array.isArray(command.target) || !command.target.length ||
      !command.target.every((part) => typeof part === "string" && part)) {
    throw new Error("stdio target must be an argv list");
  }
  transport = new StdioClientTransport({ command: command.target[0], args: command.target.slice(1) });
} else if (command.transport === "streamable-http") {
  const target = new URL(command.target);
  if (target.protocol !== "http:" || target.hostname !== "127.0.0.1" || target.pathname !== "/mcp" ||
      target.username || target.password || target.search || target.hash) {
    throw new Error("HTTP target must be loopback /mcp");
  }
  transport = new StreamableHTTPClientTransport(target);
} else {
  throw new Error("unsupported subscription transport");
}

let negotiatedVersion;
const setProtocolVersion = transport.setProtocolVersion?.bind(transport);
transport.setProtocolVersion = (version) => {
  negotiatedVersion = version;
  setProtocolVersion?.(version);
};
const client = new Client(
  { name: "mcp-statecheck", version: "0.1.0" },
  { capabilities: {}, versionNegotiation: { mode: { pin: protocol } } },
);
const events = [];
client.setNotificationHandler("notifications/tools/list_changed", (notification) => {
  events.push(notification.method);
});
let result;
try {
  await client.connect(transport);
  if (client.getProtocolEra() !== "modern" || negotiatedVersion !== protocol) {
    throw new Error("TypeScript SDK did not negotiate the modern protocol");
  }
  const subscription = await client.listen({ toolsListChanged: true });
  result = {
    events,
    honored_filter: subscription.honoredFilter,
    protocol_version: negotiatedVersion,
    runtime_version: process.versions.node,
    sdk_version: sdkVersion,
    termination: await subscription.closed,
  };
} finally {
  await client.close();
}
result.client_closed = true;
process.stdout.write(`${JSON.stringify(result)}\n`);
