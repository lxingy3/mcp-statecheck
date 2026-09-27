"""Controlled cancellation and reconnect checks for modern subscriptions."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

import anyio

from ._subscription_peer import ControlledSubscriptionHTTPPeer
from .execution import (
    _id_key,
    _normalize_inbound,
    _wire_message,
    execute_http,
    execute_stdio,
)
from .invariants import Failure, failure_signature
from .model import Action, ActionKind
from .subscription_campaign import FILTERS, SPEC, SUBSCRIPTION_ID, action, evaluate
from .transports import StdioError, StdioTransport, StreamableHTTPTransport

CHECKED = (
    Path(__file__).resolve().parents[2]
    / "artifacts"
    / "m6-subscription-lifecycle"
    / "acceptance.json"
)
REPLAYS = 10
RUNS = 2


def _command(mode: str) -> tuple[str, ...]:
    return (
        sys.executable,
        "-I",
        "-m",
        "mcp_statecheck._subscription_peer",
        "--mode",
        mode,
    )


def _normalize(messages: list[dict], request: Action) -> list[dict]:
    pending = {_id_key(request.mcp_request_id): [request.action_id]}
    return [_normalize_inbound(message, pending) for message in messages]


def _prefix(messages: list[dict], request: Action) -> list[dict]:
    events = _normalize(messages, request)
    if [event.get("method") for event in events] != [
        "notifications/subscriptions/acknowledged",
        "notifications/tools/list_changed",
    ] or evaluate((request,), events, require_close=False) is not None:
        raise RuntimeError(
            "subscription did not open with a valid acknowledgement and delivery"
        )
    return events


def _recovered(events: list[dict], request: Action) -> None:
    if (
        len(events) != 3
        or [event.get("method") for event in events[:2]]
        != [
            "notifications/subscriptions/acknowledged",
            "notifications/tools/list_changed",
        ]
        or events[-1].get("target_action_id") != request.action_id
        or evaluate((request,), events) is not None
    ):
        raise RuntimeError("replacement subscription did not complete cleanly")


def _notification_id(event: dict) -> object:
    if event.get("kind") != "notification":
        return None
    payload = event.get("payload")
    meta = payload.get("_meta") if isinstance(payload, Mapping) else None
    return meta.get(SUBSCRIPTION_ID) if isinstance(meta, Mapping) else None


def _after_cancel(first: Action, second: Action, events: list[dict]) -> Failure | None:
    for event in events:
        raw_id = _notification_id(event)
        if (
            type(raw_id) is type(first.mcp_request_id)
            and raw_id == first.mcp_request_id
        ):
            kind = "subscriptions.notification_after_cancel"
            evidence = {"subject": "server", "method": event["method"]}
            return Failure(
                kind, SPEC, first.action_id, evidence, failure_signature(kind, evidence)
            )
    return evaluate((second,), events)


async def _stdio_cancel(mode: str) -> dict:
    first, second = action(1, FILTERS[0]), action(2, FILTERS[0])
    cancel = Action(
        "cancel-1",
        ActionKind.CANCEL,
        mcp_request_id=1,
        target_action_id=first.action_id,
    )
    async with StdioTransport(_command(mode), timeout=5) as transport:
        await transport.send(_wire_message(first))
        prefix = _prefix([await transport.receive(), await transport.receive()], first)
        await transport.send(_wire_message(cancel))
        await transport.send(_wire_message(second))
        pending = {_id_key(second.mcp_request_id): [second.action_id]}
        after = []
        for _ in range(5):
            event = _normalize_inbound(await transport.receive(), pending)
            after.append(event)
            if (
                event["kind"] == "response"
                and event["target_action_id"] == second.action_id
            ):
                break
        else:
            raise RuntimeError("replacement subscription never completed")
    if transport.returncode != 0:
        raise RuntimeError("subscription stdio peer was not reaped cleanly")
    # The second subscription must succeed even when the first sends a late fault.
    replacement = [
        event
        for event in after
        if not (
            type(_notification_id(event)) is type(first.mcp_request_id)
            and _notification_id(event) == first.mcp_request_id
        )
    ]
    _recovered(replacement, second)
    failure = _after_cancel(first, second, after)
    if (mode == "late-after-cancel") != (failure is not None):
        raise RuntimeError("post-cancellation delivery classification changed")
    if (
        failure is not None
        and failure.kind != "subscriptions.notification_after_cancel"
    ):
        raise RuntimeError("late delivery had an unexpected failure kind")
    return {
        "transport": "stdio",
        "scenario": mode,
        "before_cancel": prefix,
        "after_cancel": after,
        "failure_kind": failure.kind if failure else None,
        "signature": failure.signature if failure else None,
        "cleanup": {"server_reaped": True},
    }


async def _http_cancel() -> dict:
    first, second = action(1, FILTERS[0]), action(2, FILTERS[0])
    with ControlledSubscriptionHTTPPeer("held-cancel") as peer:
        observed: list[dict] = []
        ready = anyio.Event()

        async def on_message(message: dict) -> None:
            observed.append(message)
            if len(observed) == 2:
                ready.set()

        async with StreamableHTTPTransport(peer.url, timeout=5) as transport:
            cancel_scope = anyio.CancelScope()

            async def listen() -> None:
                with cancel_scope:
                    await transport.send(_wire_message(first), on_message=on_message)

            async with anyio.create_task_group() as group:
                group.start_soon(listen)
                with anyio.fail_after(5):
                    await ready.wait()
                cancel_scope.cancel()
            prefix = _prefix(observed, first)
            if not await anyio.to_thread.run_sync(peer.cancel_seen.wait, 5):
                raise RuntimeError("HTTP peer did not observe the SSE connection close")
        if not transport._client.is_closed:
            raise RuntimeError("HTTP client did not close after cancellation")
        replacement = await execute_http((second,), peer.url, timeout=5)
        recovered = list(replacement.events)
        _recovered(recovered, second)
    if not peer.closed or replacement.cleanup.get("client_closed") is not True:
        raise RuntimeError("HTTP cancellation peer cleanup was incomplete")
    return {
        "transport": "streamable-http",
        "scenario": "held-cancel",
        "before_cancel": prefix,
        "after_cancel": recovered,
        "cleanup": {"client_closed": True, "listener_closed": True, "sse_closed": True},
    }


async def _stdio_drop() -> dict:
    first, second = action(1, FILTERS[0]), action(2, FILTERS[0])
    async with StdioTransport(_command("drop-first"), timeout=5) as transport:
        await transport.send(_wire_message(first))
        prefix = _prefix([await transport.receive(), await transport.receive()], first)
        try:
            await transport.receive()
        except StdioError as exc:
            if type(exc) is not StdioError or "stdout closed" not in str(exc):
                raise
        else:
            raise RuntimeError("stdio subscription unexpectedly completed")
    if transport.returncode != 0:
        raise RuntimeError("dropped stdio peer did not exit cleanly")
    replacement = await execute_stdio((second,), _command("conforming"), timeout=5)
    recovered = list(replacement.events)
    _recovered(recovered, second)
    if replacement.returncode != 0:
        raise RuntimeError("replacement stdio peer did not exit cleanly")
    return {
        "transport": "stdio",
        "scenario": "drop-first",
        "before_drop": prefix,
        "after_reconnect": recovered,
        "classification": "abrupt-disconnect",
        "cleanup": {"first_server_reaped": True, "replacement_server_reaped": True},
    }


async def _http_drop() -> dict:
    first, second = action(1, FILTERS[0]), action(2, FILTERS[0])
    with ControlledSubscriptionHTTPPeer("drop-first") as peer:
        async with StreamableHTTPTransport(peer.url, timeout=5) as transport:
            raw = await transport.send(_wire_message(first))
            prefix = _prefix(raw, first)
        if not transport._client.is_closed:
            raise RuntimeError("HTTP client did not close after the dropped stream")
        replacement = await execute_http((second,), peer.url, timeout=5)
        recovered = list(replacement.events)
        _recovered(recovered, second)
    if not peer.closed or replacement.cleanup.get("client_closed") is not True:
        raise RuntimeError("HTTP reconnect peer cleanup was incomplete")
    return {
        "transport": "streamable-http",
        "scenario": "drop-first",
        "before_drop": prefix,
        "after_reconnect": recovered,
        "classification": "abrupt-disconnect",
        "cleanup": {"client_closed": True, "listener_closed": True},
    }


def _run_once() -> dict[str, dict]:
    rows = {
        "stdio/cancel": anyio.run(_stdio_cancel, "held-cancel"),
        "stdio/late-after-cancel": anyio.run(_stdio_cancel, "late-after-cancel"),
        "streamable-http/cancel": anyio.run(_http_cancel),
        "stdio/drop-reconnect": anyio.run(_stdio_drop),
        "streamable-http/drop-reconnect": anyio.run(_http_drop),
    }
    fault = rows["stdio/late-after-cancel"]
    for _ in range(REPLAYS - 1):
        repeated = anyio.run(_stdio_cancel, "late-after-cancel")
        if repeated != fault:
            raise RuntimeError("late-after-cancel did not replay identically")
    fault["replays"] = REPLAYS
    return rows


def run_acceptance(output: Path, *, check: bool) -> dict:
    first = _run_once()
    if _run_once() != first:
        raise RuntimeError("subscription lifecycle campaigns were not byte-identical")
    summary = {
        "schema_version": 1,
        "milestone": "M6.4",
        "status": "passed",
        "protocol_version": "2026-07-28",
        "runs": RUNS,
        "slice": "controlled-subscription-lifecycle",
        "cells": first,
    }
    content = (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode()
    if check:
        if CHECKED.read_bytes() != content:
            raise RuntimeError(
                "subscription lifecycle evidence differs from checked artifact"
            )
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recheck M6.4 subscription cancellation and reconnect evidence"
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=CHECKED)
    args = parser.parse_args()
    summary = run_acceptance(args.output, check=args.check)
    print(
        f"M6.4 subscription lifecycle passed: {len(summary['cells'])} cells, {RUNS} identical runs, {REPLAYS} fault replays"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
