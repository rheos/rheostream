"""Core's allowlisted, read-only MCP client: JSON-RPC 2.0 over Streamable HTTP to
one configured resource URL, synchronous and bounded.

**Three requests, nothing else.** :class:`McpSession` has one public method per
request: :meth:`~McpSession.initialize` sends
:data:`~rheo_core.connectors.egress.INITIALIZE_BODY` (the discovery probe's bytes),
:meth:`~McpSession.notify_initialized` sends the ``notifications/initialized``
notification and :meth:`~McpSession.call_tool` sends one ``tools/call``. Entering
the context manager only opens the ``httpx`` client and sends nothing, so the caller
can extend its lease before, and checkpoint after, each request. The calls must come
in that order (``call_tool`` any number of times last); out of order is ``protocol``
with nothing sent.

**Allowlist first.** ``call_tool`` checks its :class:`ToolPolicy` before anything
else: a tool name outside the policy, an ``action`` outside the tool's set, a missing
or non-string ``action`` where one is required, or any ``action`` for a tool that
takes none raises :class:`ToolRefused` before the session state, the clock, the
access token or a host lookup is consulted. The name and the action must be exact
``str`` objects, and the policy runs twice: on the caller's values, then on the
values decoded back from the encoded ``params`` (where a repeated key anywhere is
refused), and those same encoded bytes are what is sent.

**Wire.** Every request carries ``Accept: application/json, text/event-stream`` and
the access token's ``Authorization`` header, and goes only to the resource URL
through :func:`~rheo_core.connectors.egress.bounded_request` (``https`` only, public
addresses only, checked before each request; no redirects; a 4 MiB cap). The
``Mcp-Session-Id`` response header and the ``protocolVersion`` the initialize result
names are sent back as ``Mcp-Session-Id`` and ``MCP-Protocol-Version`` on the later
requests. A JSON answer must be the JSON-RPC response to the request; an event
stream is read event by event and the read ends at the ``message`` event that
carries the response with the request's id (other messages are skipped). The answer
to the notification is not read past its first chunk.

**Budgets.** ``deadline`` is the caller's absolute instant on ``monotonic``. Each
request gets 50 s clipped to what is left, and none starts with under 5 s left
(``unavailable``, nothing sent); the egress reader checks the clock after the
headers and after every chunk on top of its 5 s read timeout, so a request ends
within 55 s.

**Failures.** :class:`McpFailure` codes: ``unauthorized`` (401, 403),
``rate_limited`` (429, a seconds-form ``Retry-After`` as ``retry_after``),
``unavailable`` (5xx, transport error, timeout, a body over the cap),
``endpoint_misconfigured`` (not ``https``, a non-public host, any 3xx), ``protocol``
(any other status, a malformed body, event stream or JSON-RPC envelope, including a
JSON-RPC ``error``) and ``tool_error`` (a result with ``isError: true``; its
``tool_code`` is the ``error_code`` (or, where that key is absent, the ``code``) of
the first text block whose JSON has an upper-snake code of at most 64 characters,
else ``None``; never the message). Like the egress
and OAuth failures, ``str`` and ``repr`` are the code alone and each is raised
outside any ``except`` block, so ``__cause__`` and ``__context__`` are ``None``.

**Wire trace.** With the deployment setting ``connectors.mcp_client.trace`` on (read
when the session is built), every request that was attempted logs exactly one INFO
line on the ``rheo_core.connectors.mcp_trace`` logger once it ends, in the fixed
form of :data:`TRACE_FORMAT`; ``-`` marks a field that does not apply. The fields
are only: the JSON-RPC method; for ``tools/call`` the tool name and its ``action``
(both already passed the policy); the HTTP status; the egress code and detail when
the request failed in egress (or ``not_started`` when too little time was left to
send it); the response media type (no parameters; ``other`` when it is not a plain
token pair); whether an ``Mcp-Session-Id`` header came back; on ``initialize`` the
``protocolVersion`` the server returned when it matches the version pattern, else
``missing`` or ``invalid``; a JSON-RPC ``error.code`` (an integer only); for a tool
result whether ``isError`` was true and its tool code; the number of event-stream
events and of body bytes read; and the code the request ended with (``ok`` or the
:class:`McpFailure` code). Never a header value other than the media type, a body,
a message, the token, an argument other than ``action``, or the URL. A call the
policy refuses or one out of order sends nothing and logs nothing.
"""

import json
import logging
import re
import time
from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType, TracebackType
from typing import Final, Literal, Self, final

import httpx

from rheo_core.connectors import egress
from rheo_core.connectors.egress import Clock, EgressResponse, Resolver
from rheo_core.connectors.oauth import AccessToken
from rheo_core.settings import resolve

McpCode = Literal[
    "unauthorized",
    "rate_limited",
    "unavailable",
    "endpoint_misconfigured",
    "protocol",
    "tool_error",
]
RefusalCode = Literal["tool_not_allowed", "action_not_allowed"]

CALL_BUDGET_SECONDS: Final = 50.0
MINIMUM_START_SECONDS: Final = 5.0
RESPONSE_CAP_BYTES: Final = 4 * 1024 * 1024
ACCEPT: Final = "application/json, text/event-stream"
NOTIFY_INITIALIZED_BODY: Final[bytes] = json.dumps(
    {"jsonrpc": "2.0", "method": "notifications/initialized"}, separators=(",", ":")
).encode("ascii")

_EVENT_STREAM: Final = "text/event-stream"
_JSON: Final = "application/json"
_LINE_END: Final = re.compile(rb"\r\n|\r|\n")
_BOM: Final = b"\xef\xbb\xbf"
_SESSION_ID: Final = re.compile(r"[\x21-\x7e]{1,1024}")
_PROTOCOL_VERSION: Final = re.compile(r"[0-9A-Za-z._-]{1,32}")
_TOOL_CODE: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
"""An upper-snake error code; anything else (a sentence, an identifier) is not one."""
TRACE_KEY: Final = "connectors.mcp_client.trace"
TRACE_LOGGER: Final = "rheo_core.connectors.mcp_trace"
TRACE_FORMAT: Final = (
    "mcp_trace method=%s tool=%s action=%s status=%s egress=%s content_type=%s "
    "session_id=%s protocol=%s rpc_error=%s is_error=%s tool_code=%s events=%s "
    "bytes=%s outcome=%s error_text=%s"
)
ERROR_TEXT_LIMIT: Final = 120
"""The most characters of a 4xx ``text/plain`` body the trace records."""
_trace_log: Final = logging.getLogger(TRACE_LOGGER)
_MEDIA_TOKEN: Final = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}"
)
_RPC_CODE_RANGE: Final = range(-(2**31), 2**31)
_EGRESS: Final[dict[egress.EgressCode, McpCode]] = {
    "endpoint_misconfigured": "endpoint_misconfigured",
    "timeout": "unavailable",
    "too_large": "unavailable",
    "unavailable": "unavailable",
}


class McpFailure(Exception):
    """A failed MCP request, carrying a fixed code only."""

    def __init__(
        self,
        code: McpCode,
        retry_after: int | None = None,
        *,
        tool_code: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code: McpCode = code
        self.retry_after = retry_after
        self.tool_code = tool_code

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return self.code


class ToolRefused(Exception):
    """A tool call the policy does not allow; nothing was sent."""

    def __init__(self, code: RefusalCode) -> None:
        super().__init__(code)
        self.code: RefusalCode = code

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return self.code


@final
class ToolPolicy(Mapping[str, frozenset[str] | None]):
    """A frozen map from tool name to its allowed ``action`` values; ``None`` means
    the tool takes no ``action`` argument."""

    __slots__ = ("_tools",)

    def __init__(self, tools: Mapping[str, Iterable[str] | None]) -> None:
        frozen: dict[str, frozenset[str] | None] = {}
        for name, actions in tools.items():
            if not isinstance(name, str):
                raise TypeError("tool names are strings")
            if actions is None:
                frozen[name] = None
                continue
            if isinstance(actions, str | bytes):
                raise TypeError("an action set is a set of strings, not one string")
            allowed = frozenset(actions)
            if not allowed or not all(isinstance(a, str) for a in allowed):
                raise TypeError("an action set is a non-empty set of strings")
            frozen[name] = allowed
        self._tools: Mapping[str, frozenset[str] | None] = MappingProxyType(frozen)

    def __getitem__(self, name: str) -> frozenset[str] | None:
        return self._tools[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def refusal(self, name: object, arguments: object) -> RefusalCode | None:
        """Why ``name`` with ``arguments`` is not allowed, or ``None``. The name and
        the action must be exact ``str`` objects, so a subclass cannot compare or
        hash as an allowed value while carrying other characters."""
        if type(name) is not str or name not in self._tools:
            return "tool_not_allowed"
        if not isinstance(arguments, dict):
            return "action_not_allowed"
        actions = self._tools[name]
        if actions is None:
            return "action_not_allowed" if "action" in arguments else None
        action = arguments.get("action")
        if type(action) is not str or action not in actions:
            return "action_not_allowed"
        return None


def _header(headers: httpx.Headers, name: str) -> str | None:
    try:
        value = headers.get(name)
    except (ValueError, LookupError):
        return None
    return value if isinstance(value, str) else None


def _media_type(headers: httpx.Headers) -> str:
    value = _header(headers, "content-type") or ""
    return value.split(";", 1)[0].strip().lower()


def _is_response(document: object, request_id: int) -> bool:
    """A JSON-RPC 2.0 response object for ``request_id`` (``True`` is not ``1``)."""
    if not isinstance(document, dict) or document.get("jsonrpc") != "2.0":
        return False
    found = document.get("id")
    if type(found) is not int or found != request_id:
        return False
    return ("result" in document) != ("error" in document)


def _json_message(body: bytes, request_id: int) -> dict[str, object] | None:
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return document if _is_response(document, request_id) else None


class _EventReader:
    """An incremental ``text/event-stream`` reader looking for one JSON-RPC response.

    Fed chunk by chunk while the response streams; :attr:`done` once the response
    for ``request_id`` has arrived or the stream turned out malformed.
    """

    # Its only input is the egress ``stop`` callback (``McpSession._exchange``),
    # which sees each chunk after the cap and budget checks; it never reads the
    # finished body, so a change to how egress calls ``stop`` changes this reader.

    __slots__ = (
        "_data",
        "_event",
        "_pending",
        "_request_id",
        "_started",
        "events",
        "found",
    )

    def __init__(self, request_id: int) -> None:
        self._request_id = request_id
        self._pending = bytearray()
        self._started = False
        self._event = ""
        self._data: list[str] = []
        self.events = 0
        """Events dispatched so far (any type, data present); for the trace."""
        # The response, ``False`` once the stream is malformed, else ``None``.
        self.found: dict[str, object] | None | Literal[False] = None

    @property
    def done(self) -> bool:
        return self.found is not None

    def feed(self, chunk: bytes) -> bool:
        if not self.done:
            self._pending += chunk
            self._lines(final=False)
        return self.done

    def finish(self) -> dict[str, object] | None:
        """The response once the body is complete; an unterminated event is dropped."""
        if not self.done:
            self._lines(final=True)
        return self.found or None

    def _lines(self, *, final: bool) -> None:
        buffer = self._pending
        if not self._started:
            if not final and len(buffer) < len(_BOM) and _BOM.startswith(buffer):
                return
            self._started = True
            if buffer.startswith(_BOM):
                del buffer[: len(_BOM)]
        start = 0
        while not self.done:
            match = _LINE_END.search(buffer, start)
            if match is None:
                break
            if not final and match.group() == b"\r" and match.end() == len(buffer):
                break  # the line end may be the first half of a CRLF
            self._line(bytes(buffer[start : match.start()]))
            start = match.end()
        del buffer[:start]

    def _line(self, raw: bytes) -> None:
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.found = False
            return
        if not line:
            self._dispatch()
        elif not line.startswith(":"):
            field, colon, value = line.partition(":")
            if colon and value.startswith(" "):
                value = value[1:]
            if field == "event":
                self._event = value
            elif field == "data":
                self._data.append(value)

    def _dispatch(self) -> None:
        data, event = "\n".join(self._data), self._event
        self._data, self._event = [], ""
        if not data:
            return
        self.events += 1
        if event not in ("", "message"):
            return
        try:
            document = json.loads(data)
        except (ValueError, RecursionError):
            self.found = False
            return
        if not isinstance(document, dict) or "method" in document:
            return  # a server request or notification: not ours to answer
        found = document.get("id")
        if type(found) is not int or found != self._request_id:
            return
        self.found = document if _is_response(document, self._request_id) else False


def _tool_code(result: dict[str, object]) -> str | None:
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            document = json.loads(text)
        except (ValueError, RecursionError):
            continue
        if not isinstance(document, dict):
            continue
        # Servers name the code ``error_code``; ``code`` is read only when that
        # key is absent, so an unusable ``error_code`` never lets a second field
        # stand in for it.
        code = (
            document["error_code"] if "error_code" in document else document.get("code")
        )
        if isinstance(code, str) and _TOOL_CODE.fullmatch(code):
            return code
    return None


def _decode_sent(params: bytes) -> tuple[object, object] | None:
    """The tool name and arguments exactly as the encoded ``params`` carry them,
    or ``None`` when any object in them repeats a key (a receiver could read
    either copy)."""
    repeated = False

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal repeated
        decoded = dict(items)
        repeated = repeated or len(decoded) != len(items)
        return decoded

    try:
        document = json.loads(params, object_pairs_hook=pairs)
    except (ValueError, RecursionError):
        return None
    if repeated or not isinstance(document, dict):
        return None
    return document.get("name"), document.get("arguments")


def _encode(message: dict[str, object]) -> bytes | None:
    try:
        return json.dumps(
            message, separators=(",", ":"), allow_nan=False, ensure_ascii=True
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        return None


def _status_failure(response: EgressResponse) -> McpFailure | None:
    status = response.status
    if status in (401, 403):
        return McpFailure("unauthorized")
    if status == 429:
        return McpFailure(
            "rate_limited", retry_after=egress.retry_after_seconds(response.headers)
        )
    if status >= 500:
        return McpFailure("unavailable")
    if not 200 <= status < 300:
        return McpFailure("protocol")
    return None


def _traced_media(headers: httpx.Headers) -> str:
    media = _media_type(headers)
    if not media:
        return "-"
    return media if _MEDIA_TOKEN.fullmatch(media) else "other"


def _rpc_error_code(message: dict[str, object] | None) -> int | None:
    error = None if message is None else message.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if type(code) is int and code in _RPC_CODE_RANGE else None


class _Trace:
    """The wire metadata of one request, logged once when the request ends (module
    docstring). Every field holds a fixed code, a number, a boolean or a value that
    passed a pattern or the tool policy; nothing else is ever assigned."""

    __slots__ = (
        "action",
        "body_bytes",
        "content_type",
        "egress",
        "enabled",
        "error_text",
        "events",
        "is_error",
        "method",
        "protocol",
        "rpc_error",
        "session_id",
        "status",
        "tool",
        "tool_code",
    )

    def __init__(
        self,
        enabled: bool,
        method: str,
        tool: str | None = None,
        action: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.method = method
        self.tool = tool
        self.action = action
        self.status: int | None = None
        self.egress: str | None = None
        self.content_type: str | None = None
        self.session_id: bool | None = None
        self.protocol: str | None = None
        self.rpc_error: int | None = None
        self.is_error: bool | None = None
        self.tool_code: str | None = None
        self.events: int | None = None
        self.body_bytes: int | None = None
        self.error_text: str | None = None

    def response(self, response: EgressResponse) -> None:
        self.status = response.status
        self.content_type = _traced_media(response.headers)
        self.session_id = _header(response.headers, "mcp-session-id") is not None
        self.body_bytes = len(response.body)
        if 400 <= response.status < 500 and self.content_type == "text/plain":
            self.error_text = _error_text(response.body)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self.enabled:
            return
        if exc is None:
            outcome = "ok"
        elif isinstance(exc, McpFailure):
            outcome = exc.code
        else:
            outcome = "aborted"
        _trace_log.info(
            TRACE_FORMAT,
            self.method,
            _field(self.tool),
            _field(self.action),
            _field(self.status),
            _field(self.egress),
            _field(self.content_type),
            _field(self.session_id),
            _field(self.protocol),
            _field(self.rpc_error),
            _field(self.is_error),
            _field(self.tool_code),
            _field(self.events),
            _field(self.body_bytes),
            outcome,
            "-" if self.error_text is None else json.dumps(self.error_text),
        )


_TOKEN_RUN: Final = re.compile(r"[A-Za-z0-9._~+/=-]{24,}")
"""A run long enough to be a credential; the trace never records one."""
REDACTED: Final = "[redacted]"


def _redact(text: str, secrets: tuple[str | None, ...]) -> str:
    """``text`` with every known secret, every part of one, and any long
    token-shaped run replaced."""
    for secret in secrets:
        if not secret:
            continue
        text = text.replace(secret, REDACTED)
        for part in secret.split():
            if len(part) >= 8:
                text = text.replace(part, REDACTED)
    return _TOKEN_RUN.sub(REDACTED, text)


def _error_text(body: bytes) -> str:
    """The first characters of a short plain-text error, printable ASCII only."""
    text = body.decode("utf-8", errors="replace")
    kept = "".join(c if " " <= c <= "~" else " " for c in text)
    # Not cut to the limit yet: credentials are redacted first, then it is cut.
    return " ".join(kept.split())


def _field(value: str | int | bool | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


SessionState = Literal["new", "initialized", "ready"]


class McpSession:
    """One MCP session against ``resource_url`` (module docstring)."""

    def __init__(
        self,
        resource_url: str,
        access: AccessToken,
        policy: ToolPolicy,
        *,
        deadline: float,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = egress.default_resolver,
        monotonic: Clock = time.monotonic,
    ) -> None:
        if not isinstance(policy, ToolPolicy):
            raise TypeError("McpSession needs a ToolPolicy")
        if not isinstance(access, AccessToken):
            raise TypeError("McpSession needs an AccessToken")
        self._url = resource_url
        self._access = access
        self._policy = policy
        self._deadline = deadline
        self._transport = transport
        self._resolver = resolver
        self._monotonic = monotonic
        self._client: httpx.Client | None = None
        self._state: SessionState = "new"
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._next_id = egress.INITIALIZE_REQUEST_ID + 1
        self._trace = resolve().get_bool(TRACE_KEY)

    def __enter__(self) -> Self:
        if self._client is None:
            self._client = egress.open_client(self._transport)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def initialize(self) -> None:
        """Send ``initialize``; record the session id and protocol version."""
        self._require("new")
        with _Trace(self._trace, "initialize") as trace:
            response, result = self._exchange(
                egress.INITIALIZE_BODY, egress.INITIALIZE_REQUEST_ID, trace
            )
            version = result.get("protocolVersion")
            valid = isinstance(version, str) and bool(
                _PROTOCOL_VERSION.fullmatch(version)
            )
            if "protocolVersion" not in result:
                trace.protocol = "missing"
            else:
                trace.protocol = (
                    version if valid and isinstance(version, str) else "invalid"
                )
            session_id = _header(response.headers, "mcp-session-id")
            if (
                not isinstance(version, str)
                or not valid
                or (session_id is not None and not _SESSION_ID.fullmatch(session_id))
            ):
                raise McpFailure("protocol")
        self._protocol_version = version
        self._session_id = session_id
        self._state = "initialized"

    def notify_initialized(self) -> None:
        """Send the ``notifications/initialized`` notification."""
        self._require("initialized")
        with _Trace(self._trace, "notifications/initialized") as trace:
            self._send(NOTIFY_INITIALIZED_BODY, trace, stop=lambda headers, chunk: True)
        self._state = "ready"

    def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        """One ``tools/call`` of an allowed tool; its ``result`` object."""
        snapshot = dict(arguments) if isinstance(arguments, Mapping) else None
        refusal = self._policy.refusal(name, snapshot)
        if refusal is not None:
            raise ToolRefused(refusal)
        params = _encode({"name": name, "arguments": snapshot})
        if params is None:
            raise McpFailure("protocol")
        # The policy again, on what the encoded bytes carry: these exact bytes are
        # sent, so no key or value can differ from what was checked.
        sent = _decode_sent(params)
        refusal = "action_not_allowed" if sent is None else self._policy.refusal(*sent)
        if refusal is not None:
            raise ToolRefused(refusal)
        self._require("ready")
        request_id = self._next_id
        self._next_id += 1
        body = b'{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":%s}' % (
            request_id,
            params,
        )
        # Both passed the policy: the name is an allowed tool and the action, when
        # there is one, is in that tool's set.
        action = snapshot.get("action") if snapshot is not None else None
        with _Trace(
            self._trace,
            "tools/call",
            tool=name,
            action=action if type(action) is str else None,
        ) as trace:
            _, result = self._exchange(body, request_id, trace)
            trace.is_error = result.get("isError") is True
            if trace.is_error:
                trace.tool_code = _tool_code(result)
                raise McpFailure("tool_error", tool_code=trace.tool_code)
        return result

    def _require(self, state: SessionState) -> None:
        if self._client is None or self._state != state:
            raise McpFailure("protocol")

    def _exchange(
        self, body: bytes, request_id: int, trace: _Trace
    ) -> tuple[EgressResponse, dict[str, object]]:
        """Send one request and return its JSON-RPC ``result`` object."""
        reader = _EventReader(request_id)

        def stop(headers: httpx.Headers, chunk: bytes) -> bool:
            return _media_type(headers) == _EVENT_STREAM and reader.feed(chunk)

        response = self._send(body, trace, stop=stop)
        media = _media_type(response.headers)
        message: dict[str, object] | None = None
        if media == _JSON:
            message = _json_message(response.body, request_id)
        elif media == _EVENT_STREAM:
            message = reader.finish()
            trace.events = reader.events
        trace.rpc_error = _rpc_error_code(message)
        result = None if message is None else message.get("result")
        if not isinstance(result, dict):
            raise McpFailure("protocol")
        return response, result

    def _send(self, body: bytes, trace: _Trace, *, stop: egress.Stop) -> EgressResponse:
        """One bounded POST to the resource URL; a 2xx response or a failure."""
        client = self._client
        remaining = self._deadline - self._monotonic()
        if client is None or remaining < MINIMUM_START_SECONDS:
            trace.egress = "not_started"
            raise McpFailure("unavailable")
        headers = {
            "Content-Type": _JSON,
            "Accept": ACCEPT,
            "Authorization": self._access.authorization_header(),
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version is not None:
            headers["MCP-Protocol-Version"] = self._protocol_version
        outcome: EgressResponse | McpCode
        try:
            outcome = egress.bounded_request(
                client,
                "POST",
                self._url,
                budget=min(CALL_BUDGET_SECONDS, remaining),
                cap=RESPONSE_CAP_BYTES,
                resolver=self._resolver,
                clock=self._monotonic,
                headers=headers,
                content=body,
                stop=stop,
            )
        except egress.EgressFailure as failure:
            outcome = _EGRESS[failure.code]
            trace.egress = failure.code
            if failure.detail is not None:
                trace.egress += "/" + failure.detail
            trace.status = failure.status
        if not isinstance(outcome, EgressResponse):
            raise McpFailure(outcome)
        trace.response(outcome)
        if trace.error_text is not None:
            trace.error_text = _redact(
                trace.error_text,
                (headers["Authorization"], self._session_id),
            )[:ERROR_TEXT_LIMIT]
        failed = _status_failure(outcome)
        if failed is not None:
            raise failed
        return outcome
