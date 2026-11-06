// Pinned TypeScript SDK subscription probe, run in its isolated environment.

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

const protocol = "2026-07-28";
const resource = "file:///controlled/config.json";
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
    !["graceful", "cancel", "drop", "concurrent-first", "concurrent-second"].includes(scenario)) {
  throw new Error("invalid subscription probe command");
}

async function concurrent(client, transport, cancelledIndex) {
  const ids = [];
  const events = [];
  const initial = Promise.withResolvers();
  let phase = "before_cancel";
  const send = transport.send.bind(transport);
  // Observe public transport messages without changing SDK correlation or cancellation.
  transport.send = (message, options) => {
    if (message.method === "subscriptions/listen") ids.push(message.id);
    return send(message, options);
  };
  for (const method of ["notifications/tools/list_changed", "notifications/resources/updated"]) {
    client.setNotificationHandler(method, (notification) => {
      const event = { type: notification.method };
      if (method === "notifications/resources/updated") event.uri = notification.params.uri;
      events.push({ id: notification.params?._meta?.["io.modelcontextprotocol/subscriptionId"], phase, event });
      if (events.length === 2) initial.resolve();
    });
  }
  try {
    const subscriptions = [
      await client.listen({ toolsListChanged: true }),
      await client.listen({ resourceSubscriptions: [resource] }),
    ];
    if (ids.length !== 2 || ids[0] === ids[1]) throw new Error("concurrent subscriptions reused an ID");
    await client.listTools(undefined, { cacheMode: "refresh" });
    await initial.promise;
    await subscriptions[cancelledIndex].close();
    const cancelled = await subscriptions[cancelledIndex].closed;
    phase = "after_cancel";
    const tools = await client.listTools(undefined, { cacheMode: "refresh" });
    const survivor = await subscriptions[1 - cancelledIndex].closed;
    // A graceful close with a missing final event must fail rather than hang.
    if (events.length < 3) throw new Error("surviving subscription did not deliver after cancellation");
    if (events.some(({ id }) => !ids.includes(id))) throw new Error("notification used an unknown subscription ID");
    return {
      distinct_ids: true,
      cancelled_index: cancelledIndex,
      refetched_tools: tools.tools.map((tool) => tool.name),
      subscriptions: subscriptions.map((sub, index) => ({
        honored_filter: sub.honoredFilter,
        before_cancel: events.filter((item) => item.id === ids[index] && item.phase === "before_cancel").map((item) => item.event),
        after_cancel: events.filter((item) => item.id === ids[index] && item.phase === "after_cancel").map((item) => item.event),
        termination: index === cancelledIndex ? cancelled : survivor,
      })),
    };
  } finally {
    transport.send = send;
  }
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
    if (scenario === "concurrent-first" || scenario === "concurrent-second") {
      result = await concurrent(client, transport, Number(scenario === "concurrent-second"));
    } else {
      const subscription = await client.listen({ toolsListChanged: true });
      if (scenario === "cancel") {
        await firstEvent.promise;
        await subscription.close();
      }
      const termination = await subscription.closed;
      result = {
        events: [...events],
        honored_filter: subscription.honoredFilter,
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
    }
    Object.assign(result, {
      protocol_version: negotiatedVersion,
      runtime_version: process.versions.node,
      sdk_version: sdkVersion,
    });
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
