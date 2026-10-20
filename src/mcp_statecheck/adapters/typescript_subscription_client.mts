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
const scenario = "scenario" in command ? command.scenario : "graceful";
const keys = ["target", "transport"];
if ("scenario" in command) keys.push("scenario");
if (scenario === "drop") keys.push("recovery_target");
if (Object.keys(command).sort().join(",") !== keys.sort().join(",") ||
    !["graceful", "cancel", "drop"].includes(scenario)) {
  throw new Error("invalid subscription probe command");
}

async function probe(transportKind, target, scenario) {
  let transport;
  if (transportKind === "stdio") {
    if (!Array.isArray(target) || !target.length ||
        !target.every((part) => typeof part === "string" && part)) {
      throw new Error("stdio target must be an argv list");
    }
    transport = new StdioClientTransport({ command: target[0], args: target.slice(1) });
  } else if (transportKind === "streamable-http") {
    if (typeof target !== "string") throw new Error("HTTP target must be a URL");
    const url = new URL(target);
    if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.pathname !== "/mcp" ||
        url.username || url.password || url.search || url.hash) {
      throw new Error("HTTP target must be loopback /mcp");
    }
    transport = new StreamableHTTPClientTransport(url);
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
  const firstEvent = Promise.withResolvers();
  client.setNotificationHandler("notifications/tools/list_changed", (notification) => {
    events.push(notification.method);
    firstEvent.resolve();
  });
  let result;
  try {
    await client.connect(transport);
    if (client.getProtocolEra() !== "modern" || negotiatedVersion !== protocol) {
      throw new Error("TypeScript SDK did not negotiate the modern protocol");
    }
    const subscription = await client.listen({ toolsListChanged: true });
    if (scenario === "cancel") {
      await firstEvent.promise;
      await subscription.close();
    }
    const termination = await subscription.closed;
    result = {
      events: [...events],
      honored_filter: subscription.honoredFilter,
      protocol_version: negotiatedVersion,
      runtime_version: process.versions.node,
      sdk_version: sdkVersion,
      termination,
    };
    if (scenario === "cancel") {
      events.length = 0;
      const replacement = await client.listen({ toolsListChanged: true });
      const termination = await replacement.closed;
      result.replacement = {
        events: [...events],
        honored_filter: replacement.honoredFilter,
        termination,
      };
    }
    if (scenario === "cancel" || scenario === "refetch") {
      result.refetched_tools = (await client.listTools(undefined, { cacheMode: "refresh" })).tools.map((tool) => tool.name);
    }
  } finally {
    await client.close();
  }
  result.client_closed = true;
  return result;
}

const result = await probe(command.transport, command.target, scenario);
if (scenario === "drop") {
  result.replacement = await probe(command.transport, command.recovery_target, "refetch");
}
process.stdout.write(`${JSON.stringify(result)}\n`);
