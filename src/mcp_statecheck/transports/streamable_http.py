"""MCP Streamable HTTP transport with resumable SSE parsing."""

from __future__ import annotations

import codecs
import json
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from typing import Any

import anyio
import httpx

ACCEPT = "application/json, text/event-stream"
MAX_SSE_RETRY_MS = 2**31 - 1


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_json_loads(value: str | bytes) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite,
    )


class HTTPTransportError(Exception):
    """Base error for Streamable HTTP."""


class HTTPTimeout(HTTPTransportError, TimeoutError):
    """An HTTP operation exceeded its deadline."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HTTPProtocolError(HTTPTransportError):
    """The endpoint returned invalid transport data."""


class HTTPStatusError(HTTPTransportError):
    """The endpoint returned a non-success status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class Unauthorized(HTTPStatusError):
    """The endpoint returned HTTP 401."""


class Forbidden(HTTPStatusError):
    """The endpoint returned HTTP 403."""


class ServerError(HTTPStatusError):
    """The endpoint returned a 5xx status."""


class SessionExpired(HTTPStatusError):
    """A request used a session that the endpoint no longer recognizes."""

    def __init__(self, session_id: str) -> None:
        super().__init__(404, "MCP session expired")
        self.session_id = session_id


@dataclass(frozen=True, slots=True)
class SSEEvent:
    data: str
    event: str = "message"
    event_id: str = ""
    retry: int | None = None


class SSEParser:
    """Incrementally parse one UTF-8 event stream."""

    def __init__(
        self, *, last_event_id: str = "", max_event_chars: int = 1024 * 1024
    ) -> None:
        self.last_event_id = last_event_id
        self.id_seen = False
        self.max_event_chars = max_event_chars
        # Event streams ignore one UTF-8 BOM at the start. The incremental
        # codec also handles a BOM split across network chunks.
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self._text = ""
        self._data: list[str] = []
        self._data_chars = 0
        self._event = ""
        self._retry: int | None = None

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        try:
            self._text += self._decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            raise HTTPProtocolError("SSE stream is not valid UTF-8") from exc
        return self._consume_lines(final=False)

    @property
    def retry(self) -> int | None:
        return self._retry

    def finish(self) -> list[SSEEvent]:
        try:
            self._text += self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise HTTPProtocolError("SSE stream ended inside a UTF-8 sequence") from exc
        events = self._consume_lines(final=True)
        self._data = []
        self._data_chars = 0
        self._event = ""
        return events

    def _consume_lines(self, *, final: bool) -> list[SSEEvent]:
        events: list[SSEEvent] = []
        start = 0
        index = 0
        while index < len(self._text):
            char = self._text[index]
            if char not in "\r\n":
                index += 1
                continue
            if char == "\r" and index + 1 == len(self._text) and not final:
                break
            line = self._text[start:index]
            if (
                char == "\r"
                and index + 1 < len(self._text)
                and self._text[index + 1] == "\n"
            ):
                index += 1
            event = self._process_line(line)
            if event is not None:
                events.append(event)
            index += 1
            start = index

        self._text = self._text[start:]
        if len(self._text) > self.max_event_chars:
            raise HTTPProtocolError("SSE line exceeds the configured limit")
        if final and self._text:
            event = self._process_line(self._text)
            self._text = ""
            if event is not None:
                events.append(event)
        return events

    def _process_line(self, line: str) -> SSEEvent | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data_chars += len(value)
            if self._data_chars > self.max_event_chars:
                raise HTTPProtocolError("SSE event exceeds the configured limit")
            self._data.append(value)
        elif field == "event":
            self._event = value
        elif field == "id" and "\x00" not in value:
            self.last_event_id = value
            self.id_seen = True
        elif field == "retry" and value and all("0" <= char <= "9" for char in value):
            try:
                retry = int(value)
            except ValueError as exc:
                raise HTTPProtocolError("SSE retry value is too large") from exc
            if retry > MAX_SSE_RETRY_MS:
                raise HTTPProtocolError("SSE retry value exceeds the supported range")
            self._retry = retry
        return None

    def _dispatch(self) -> SSEEvent | None:
        if not self._data:
            self._event = ""
            return None
        event = SSEEvent(
            data="\n".join(self._data),
            event=self._event or "message",
            event_id=self.last_event_id,
            retry=self._retry,
        )
        self._data = []
        self._data_chars = 0
        self._event = ""
        return event


class StreamableHTTPTransport:
    """Send individual JSON-RPC messages to one MCP HTTP endpoint."""

    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 5.0,
        protocol_version: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if timeout <= 0 or max_response_bytes < 1:
            raise ValueError("timeout and response limit must be positive")
        self.url = url
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.session_id: str | None = None
        self.protocol_version = protocol_version
        self._headers = httpx.Headers(headers)
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        )
        self._cursors: dict[str, str] = {}
        self._retries: dict[str, int] = {}
        self._closed = False

    async def __aenter__(self) -> StreamableHTTPTransport:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def cursors(self) -> dict[str, str]:
        return dict(self._cursors)

    def cursor(self, stream: str = "default") -> str | None:
        return self._cursors.get(stream)

    def retry(self, stream: str = "default") -> int | None:
        return self._retries.get(stream)

    def set_protocol_version(self, version: str) -> None:
        if not version:
            raise ValueError("protocol version must not be empty")
        self.protocol_version = version

    async def send(
        self, message: Mapping[str, Any], *, stream: str = "default"
    ) -> list[dict[str, Any]]:
        self._ensure_open()
        method = message.get("method")
        is_initialize = method == "initialize"
        is_request = "method" in message and "id" in message
        if is_initialize:
            self.session_id = None
            self.protocol_version = None
        sent_session = None if is_initialize else self.session_id
        response_status: int | None = None
        staged_session: str | None = None
        initialize_succeeded = False
        messages: list[dict[str, Any]]
        headers = self._request_headers(
            include_protocol=not is_initialize, include_session=not is_initialize
        )
        headers["Content-Type"] = "application/json"
        try:
            payload = json.dumps(
                dict(message),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise HTTPProtocolError("outbound message is not valid JSON") from exc
        try:
            with anyio.fail_after(self.timeout):
                async with self._client.stream(
                    "POST", self.url, headers=headers, content=payload
                ) as response:
                    response_status = response.status_code
                    self._raise_for_status(response, sent_session)
                    expected_status = 200 if is_request else 202
                    if response.status_code != expected_status:
                        raise HTTPProtocolError(
                            f"JSON-RPC {'request' if is_request else 'message'} "
                            f"requires HTTP {expected_status}, got {response.status_code}"
                        )
                    if is_initialize and "MCP-Session-Id" in response.headers:
                        staged_session = response.headers["MCP-Session-Id"]
                        if not staged_session or not all(
                            "!" <= char <= "~" for char in staged_session
                        ):
                            raise HTTPProtocolError(
                                "MCP session ID must contain only visible ASCII"
                            )
                    messages = await self._read_response(
                        response,
                        stream,
                        request_id=message.get("id") if is_request else None,
                        expects_response=is_request,
                    )
                    if is_initialize and is_request:
                        initialize_succeeded = self._initialize_succeeded(
                            messages, message["id"]
                        )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise HTTPTimeout(
                "HTTP POST timed out", status_code=response_status
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPTransportError(f"HTTP POST failed: {exc}") from exc
        if initialize_succeeded:
            self.session_id = staged_session
        return messages

    async def resume(self, stream: str = "default") -> list[dict[str, Any]]:
        messages = self.iter_messages(stream)
        try:
            with anyio.fail_after(self.timeout):
                try:
                    return [await anext(messages)]
                except StopAsyncIteration:
                    return []
        except HTTPTimeout:
            raise
        except TimeoutError as exc:
            raise HTTPTimeout("HTTP GET timed out") from exc
        finally:
            await messages.aclose()

    async def iter_messages(
        self, stream: str = "default"
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield messages from one resumable GET SSE connection."""
        self._ensure_open()
        sent_session = self.session_id
        response_status: int | None = None
        response: httpx.Response | None = None
        headers = self._request_headers(include_protocol=True, include_session=True)
        cursor = self._cursors.get(stream)
        if cursor:
            headers = self._with_utf8_header(headers, "Last-Event-ID", cursor)
        try:
            try:
                retry = self._retries.get(stream)
                if retry:
                    with anyio.fail_after(self.timeout):
                        await anyio.sleep(retry / 1000)
                request = self._client.build_request("GET", self.url, headers=headers)
                with anyio.fail_after(self.timeout):
                    response = await self._client.send(request, stream=True)
                response_status = response.status_code
                self._raise_for_status(response, sent_session)
                if response.status_code != 200:
                    raise HTTPProtocolError(
                        f"GET SSE requires HTTP 200, got {response.status_code}"
                    )
                content_type = (
                    response.headers.get("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if content_type != "text/event-stream":
                    raise HTTPProtocolError("GET response is not an SSE stream")
                async for message in self._iter_sse(response, stream):
                    yield message
            except BaseException:
                if response is not None:
                    with anyio.CancelScope(shield=True):
                        with anyio.move_on_after(self.timeout):
                            await response.aclose()
                raise
            else:
                if response is not None:
                    with anyio.fail_after(self.timeout):
                        await response.aclose()
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise HTTPTimeout(
                "HTTP GET timed out", status_code=response_status
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPTransportError(f"HTTP GET failed: {exc}") from exc

    async def close(self) -> None:
        if self._closed:
            return
        error: BaseException | None = None
        sent_session = self.session_id
        client_closed = False
        with anyio.CancelScope(shield=True):
            try:
                if sent_session is not None:
                    headers = self._request_headers(
                        include_protocol=True, include_session=True
                    )
                    with anyio.fail_after(self.timeout):
                        response = await self._client.delete(self.url, headers=headers)
                        if response.status_code not in {404, 405}:
                            self._raise_for_status(response, sent_session)
            except (TimeoutError, httpx.TimeoutException) as exc:
                error = HTTPTimeout("HTTP DELETE timed out")
                error.__cause__ = exc
            except BaseException as exc:
                error = exc
            try:
                with anyio.fail_after(self.timeout):
                    await self._client.aclose()
                client_closed = True
            except (TimeoutError, httpx.TimeoutException) as exc:
                if error is None:
                    error = HTTPTimeout("HTTP client cleanup timed out")
                    error.__cause__ = exc
            except BaseException as exc:
                if error is None:
                    error = exc
            if client_closed:
                self.session_id = None
                self._closed = True
        if error is not None:
            raise error

    async def aclose(self) -> None:
        await self.close()

    async def _read_response(
        self,
        response: httpx.Response,
        stream: str,
        *,
        request_id: Any,
        expects_response: bool,
    ) -> list[dict[str, Any]]:
        if not expects_response:
            if await self._read_limited(response):
                raise HTTPProtocolError("HTTP 202 response must have an empty body")
            return []
        content_type = (
            response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type == "text/event-stream":
            return await self._read_sse(response, stream, request_id)
        if content_type != "application/json":
            raise HTTPProtocolError(
                f"unsupported response content type: {content_type or 'missing'}"
            )
        body = await self._read_limited(response)
        if not body:
            raise HTTPProtocolError("JSON response body is empty")
        try:
            message = _strict_json_loads(body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPProtocolError("response body is not valid JSON") from exc
        if not isinstance(message, dict):
            raise HTTPProtocolError("response body must be a JSON object")
        return [message]

    async def _read_sse(
        self, response: httpx.Response, stream: str, request_id: Any
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        async for message in self._iter_sse(response, stream):
            messages.append(message)
            if self._is_response_for(message, request_id):
                break
        return messages

    async def _iter_sse(
        self, response: httpx.Response, stream: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        parser = SSEParser(
            last_event_id=self._cursors.get(stream, ""),
            max_event_chars=self.max_response_bytes,
        )
        total_bytes = 0
        chunks = response.aiter_bytes()
        while True:
            try:
                with anyio.fail_after(self.timeout):
                    chunk = await anext(chunks)
            except StopAsyncIteration:
                break
            total_bytes += len(chunk)
            if total_bytes > self.max_response_bytes:
                raise HTTPProtocolError("SSE stream exceeds the configured limit")
            for event in parser.feed(chunk):
                self._store_event_state(stream, event, parser.id_seen)
                if event.data == "":
                    continue
                yield self._decode_event(event)
            self._store_retry(stream, parser)
        for event in parser.finish():
            self._store_event_state(stream, event, parser.id_seen)
            if event.data == "":
                continue
            yield self._decode_event(event)
        self._store_retry(stream, parser)

    async def _read_limited(self, response: httpx.Response) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > self.max_response_bytes:
                raise HTTPProtocolError("response body exceeds the configured limit")
        return bytes(body)

    @staticmethod
    def _decode_event(event: SSEEvent) -> dict[str, Any]:
        try:
            message = _strict_json_loads(event.data)
        except ValueError as exc:
            raise HTTPProtocolError("SSE data is not valid JSON") from exc
        if not isinstance(message, dict):
            raise HTTPProtocolError("SSE data must be a JSON object")
        return message

    @staticmethod
    def _is_response_for(message: Mapping[str, Any], request_id: Any) -> bool:
        return (
            message.get("jsonrpc") == "2.0"
            and "method" not in message
            and "id" in message
            and not isinstance(message["id"], bool)
            and not isinstance(request_id, bool)
            and message["id"] == request_id
            and (("result" in message) != ("error" in message))
        )

    def _initialize_succeeded(
        self, messages: list[dict[str, Any]], request_id: Any
    ) -> bool:
        for message in messages:
            if self._is_response_for(message, request_id):
                if "error" in message:
                    return False
                result = message["result"]
                server_info = (
                    result.get("serverInfo") if isinstance(result, dict) else None
                )
                if not (
                    isinstance(result, dict)
                    and isinstance(result.get("protocolVersion"), str)
                    and isinstance(result.get("capabilities"), dict)
                    and isinstance(server_info, dict)
                    and isinstance(server_info.get("name"), str)
                    and isinstance(server_info.get("version"), str)
                ):
                    raise HTTPProtocolError("initialize result has an invalid shape")
                return True
        raise HTTPProtocolError("initialize response is missing or has a mismatched ID")

    def _store_event_state(self, stream: str, event: SSEEvent, id_seen: bool) -> None:
        if id_seen:
            if event.event_id:
                self._cursors[stream] = event.event_id
            else:
                self._cursors.pop(stream, None)
        if event.retry is not None:
            self._retries[stream] = event.retry

    def _store_retry(self, stream: str, parser: SSEParser) -> None:
        if parser.retry is not None:
            self._retries[stream] = parser.retry

    def _request_headers(
        self, *, include_protocol: bool, include_session: bool
    ) -> httpx.Headers:
        headers = self._headers.copy()
        for reserved in (
            "MCP-Session-Id",
            "MCP-Protocol-Version",
            "Last-Event-ID",
        ):
            headers.pop(reserved, None)
        headers["Accept"] = ACCEPT
        if include_session and self.session_id is not None:
            headers["MCP-Session-Id"] = self.session_id
        if include_protocol and self.protocol_version is not None:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    @staticmethod
    def _with_utf8_header(
        headers: httpx.Headers, name: str, value: str
    ) -> httpx.Headers:
        try:
            raw_name = name.encode("ascii")
            raw_value = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HTTPProtocolError(f"{name} cannot be encoded") from exc
        return httpx.Headers([*headers.raw, (raw_name, raw_value)])

    def _raise_for_status(
        self, response: httpx.Response, sent_session: str | None
    ) -> None:
        status = response.status_code
        if status < 300:
            return
        if status == 404 and sent_session is not None:
            if self.session_id == sent_session:
                self.session_id = None
            raise SessionExpired(sent_session)
        if status == 401:
            raise Unauthorized(status, "HTTP endpoint returned 401 Unauthorized")
        if status == 403:
            raise Forbidden(status, "HTTP endpoint returned 403 Forbidden")
        if 500 <= status <= 599:
            raise ServerError(status, f"HTTP endpoint returned {status}")
        raise HTTPStatusError(status, f"HTTP endpoint returned {status}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise HTTPTransportError("transport is closed")
