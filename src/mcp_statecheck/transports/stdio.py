"""Newline-delimited JSON transport over a direct child process."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

import anyio
from anyio.abc import ByteReceiveStream, Process, TaskGroup
from anyio.streams.buffered import BufferedByteReceiveStream


class StdioError(Exception):
    """Base error for the stdio transport."""


class StdioTimeout(StdioError, TimeoutError):
    """A stdio operation exceeded its deadline."""


class StdioProtocolError(StdioError):
    """The child emitted an invalid JSON line."""


class StdioTransport:
    """Exchange JSON objects with one directly spawned process."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 5.0,
        shutdown_timeout: float = 1.0,
        stderr_limit: int = 64 * 1024,
        max_message_bytes: int = 1024 * 1024,
    ) -> None:
        if isinstance(command, (str, bytes)):
            raise TypeError("command must be an argv sequence, not a shell string")
        self.command = tuple(command)
        if not self.command or not all(isinstance(arg, str) for arg in self.command):
            raise ValueError("command must contain at least one string argument")
        if timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if stderr_limit < 0 or max_message_bytes < 1:
            raise ValueError("stream limits are invalid")

        self.cwd = None if cwd is None else os.fspath(cwd)
        if self.cwd is not None and not isinstance(self.cwd, str):
            raise TypeError("cwd must be a string path")
        self.env = None if env is None else dict(env)
        if self.env is not None and not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self.env.items()
        ):
            raise TypeError("environment keys and values must be strings")

        self.timeout = timeout
        self.shutdown_timeout = shutdown_timeout
        self.stderr_limit = stderr_limit
        self.max_message_bytes = max_message_bytes
        self._process: Process | None = None
        self._stdout: BufferedByteReceiveStream | None = None
        self._tasks: TaskGroup | None = None
        self._stderr = bytearray()
        self._stderr_truncated = False
        self._closed = False
        self._last_returncode: int | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def returncode(self) -> int | None:
        if self._process is not None:
            return self._process.returncode
        return self._last_returncode

    @property
    def stderr(self) -> str:
        prefix = "[truncated]\n" if self._stderr_truncated else ""
        return prefix + self._stderr.decode("utf-8", errors="replace")

    async def __aenter__(self) -> StdioTransport:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._process is not None:
            return
        if self._closed:
            raise StdioError("transport is closed")

        try:
            with anyio.fail_after(self.timeout):
                process = await anyio.open_process(
                    self.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.cwd,
                    env=self.env,
                )
        except TimeoutError as exc:
            raise StdioTimeout("starting the child timed out") from exc
        except OSError as exc:
            raise StdioError(f"could not start child: {exc}") from exc

        self._process = process
        assert process.stdout is not None and process.stderr is not None
        self._stdout = BufferedByteReceiveStream(process.stdout)
        try:
            self._tasks = anyio.create_task_group()
            await self._tasks.__aenter__()
            self._tasks.start_soon(self._drain_stderr, process.stderr)
        except BaseException:
            await self.close()
            raise

    async def send(self, message: Mapping[str, Any]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise StdioError("child stdin is unavailable")
        try:
            payload = (
                json.dumps(
                    dict(message),
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as exc:
            raise StdioProtocolError(
                f"message is not JSON serializable: {exc}"
            ) from exc
        if len(payload) > self.max_message_bytes:
            raise StdioProtocolError("message exceeds the configured byte limit")

        try:
            with anyio.fail_after(self.timeout):
                await process.stdin.send(payload)
        except TimeoutError as exc:
            await self.close()
            raise StdioTimeout("writing to the child timed out") from exc
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            await self.close()
            raise StdioError("child stdin closed") from exc

    async def receive(self) -> dict[str, Any]:
        self._require_process()
        assert self._stdout is not None
        try:
            with anyio.fail_after(self.timeout):
                line = await self._stdout.receive_until(b"\n", self.max_message_bytes)
        except TimeoutError as exc:
            await self.close()
            raise StdioTimeout("reading from the child timed out") from exc
        except anyio.DelimiterNotFound as exc:
            await self.close()
            raise StdioProtocolError("child output exceeded the line limit") from exc
        except (anyio.EndOfStream, anyio.IncompleteRead) as exc:
            await self.close()
            raise StdioError("child stdout closed before a complete message") from exc

        try:
            message = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            await self.close()
            raise StdioProtocolError(f"child emitted invalid JSON: {exc}") from exc
        if not isinstance(message, dict):
            await self.close()
            raise StdioProtocolError("child message must be a JSON object")
        return message

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        cleanup_error: StdioError | None = None

        if self._tasks is not None:
            self._tasks.cancel_scope.shield = True
        if process is not None:
            if process.stdin is not None:
                with anyio.move_on_after(self.shutdown_timeout):
                    await process.stdin.aclose()

            if not await self._wait(process):
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                if not await self._wait(process):
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    if not await self._wait(process):
                        cleanup_error = StdioError("child could not be reaped")

            self._last_returncode = process.returncode
            if self._stdout is not None:
                with anyio.move_on_after(self.shutdown_timeout):
                    await self._stdout.aclose()
            try:
                with anyio.fail_after(self.shutdown_timeout):
                    await process.aclose()
            except TimeoutError:
                cleanup_error = StdioError("child pipes could not be closed")

        if self._tasks is not None:
            self._tasks.cancel_scope.cancel()
            await self._tasks.__aexit__(None, None, None)

        self._process = None
        self._stdout = None
        self._tasks = None
        if cleanup_error is not None:
            raise cleanup_error

    async def _wait(self, process: Process) -> bool:
        try:
            with anyio.fail_after(self.shutdown_timeout):
                self._last_returncode = await process.wait()
            return True
        except TimeoutError:
            return False

    async def _drain_stderr(self, stream: ByteReceiveStream) -> None:
        try:
            while True:
                chunk = await stream.receive(64 * 1024)
                if not self.stderr_limit:
                    self._stderr_truncated = self._stderr_truncated or bool(chunk)
                    continue
                if len(chunk) >= self.stderr_limit:
                    self._stderr[:] = chunk[-self.stderr_limit :]
                    self._stderr_truncated = True
                    continue
                self._stderr.extend(chunk)
                overflow = len(self._stderr) - self.stderr_limit
                if overflow > 0:
                    del self._stderr[:overflow]
                    self._stderr_truncated = True
        except (
            anyio.EndOfStream,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ):
            return

    def _require_process(self) -> Process:
        if self._process is None or self._closed:
            raise StdioError("transport is not running")
        return self._process


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")
