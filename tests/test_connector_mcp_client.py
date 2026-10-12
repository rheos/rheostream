"""Core's allowlisted MCP client: three explicit one-request methods, the tool
policy checked before anything is sent, JSON and event-stream answers, per-request
budgets under the caller's deadline, public addresses only, and content-free
failures.

A fake MCP server sits behind a counting ``httpx.MockTransport``; host names go to
an injected resolver and every budget is measured on a fake monotonic clock, so no
test touches the network.
"""

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx
import pytest
from rheo_core.connectors import egress, mcp_client
from rheo_core.connectors.mcp_client import (
    McpFailure,
    McpSession,
    ToolPolicy,
    ToolRefused,
)
from rheo_core.connectors.oauth import AccessToken

PUBLIC = "2600::1"
"""A global unicast address; no test resolves or contacts it."""
RESOURCE = "https://mcp.example.com/mcp"
ACCESS = "access-marker-6b2e9d14-3c7a-4f58-9e01-2d4c6a8b0f73"
ORG = "org-marker-8a4c2e61-7d3b-4f95-b0e2-1c5a9f7d3e48"
MARKERS = (ACCESS, ORG)
POLICY = ToolPolicy(
    {"list_accounts": None, "find_jobs": {"smart_search", "search", "get"}}
)
PERMITTED = {"initialize", "notifications/initialized", "tools/call"}
TIMEOUT_EXTENSION = {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}

NON_PUBLIC = {
    "loopback": "127.0.0.1",
    "rfc1918": "10.1.2.3",
    "link-local": "169.254.169.254",
    "unique-local": "fd00::5",
    "unspecified": "0.0.0.0",
    "reserved": "240.0.0.9",
    "multicast": "239.1.1.1",
}

Handler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Trickle(httpx.SyncByteStream):
    """One small chunk every ``interval`` seconds of fake time, forever."""

    def __init__(
        self, clock: FakeClock, interval: float, chunk: bytes = b":\n"
    ) -> None:
        self.clock = clock
        self.interval = interval
        self.chunk = chunk
        self.sent = 0

    def __iter__(self) -> Iterator[bytes]:
        while True:
            self.clock.advance(self.interval)
            self.sent += 1
            yield self.chunk


class Chunks(httpx.SyncByteStream):
    """The given chunks, then (when ``then`` is set) that stream's chunks."""

    def __init__(
        self, chunks: Sequence[bytes], then: httpx.SyncByteStream | None = None
    ) -> None:
        self.chunks = chunks
        self.then = then
        self.sent = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.sent += 1
            yield chunk
        if self.then is not None:
            yield from self.then


def rpc(request_id: object, result: object) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def tool_result(document: object, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(document)}],
        "isError": is_error,
    }


def event_stream(*chunks: bytes, then: httpx.SyncByteStream | None = None) -> Any:
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        stream=Chunks(list(chunks), then),
    )


def sse_message(document: object) -> bytes:
    return b"event: message\ndata: " + json.dumps(document).encode() + b"\n\n"


class FakeMcp:
    """A counting fake MCP server; each method's answer can be replaced."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.requests: list[httpx.Request] = []
        self.lookups: list[str] = []
        self.addresses: dict[str, str] = {}
        self.session_id: str | None = "session-7f3a"
        self.initialize: Handler = self.initialized
        self.notify: Handler = lambda request: httpx.Response(202)
        self.tool: Handler = lambda request: httpx.Response(
            200, json=rpc(_body(request)["id"], tool_result({"jobs": []}))
        )

    def initialized(self, request: httpx.Request) -> httpx.Response:
        headers = {} if self.session_id is None else {"Mcp-Session-Id": self.session_id}
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        return httpx.Response(200, headers=headers, json=rpc(1, result))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = _body(request)["method"]
        if method == "initialize":
            return self.initialize(request)
        if method == "notifications/initialized":
            return self.notify(request)
        return self.tool(request)

    def resolve(self, host: str) -> Sequence[str]:
        self.lookups.append(host)
        return [self.addresses.get(host, PUBLIC)]

    def session(self, *, deadline: float = 420.0, url: str = RESOURCE) -> McpSession:
        return McpSession(
            url,
            AccessToken(ACCESS),
            POLICY,
            deadline=deadline,
            transport=httpx.MockTransport(self.handle),
            resolver=self.resolve,
            monotonic=self.clock,
        )

    def methods(self) -> list[str]:
        return [_body(r)["method"] for r in self.requests]


def _body(request: httpx.Request) -> dict[str, Any]:
    document = json.loads(request.content)
    assert isinstance(document, dict)
    return document


def ready(session: McpSession) -> McpSession:
    session.initialize()
    session.notify_initialized()
    return session


def mcp_failure(fn: Callable[[], object]) -> McpFailure:
    with pytest.raises(McpFailure) as caught:
        fn()
    return caught.value


def assert_content_free(exc: BaseException, code: str) -> None:
    assert str(exc) == code
    assert repr(exc) == code
    assert exc.__cause__ is None
    assert exc.__context__ is None
    for marker in MARKERS:
        assert marker not in str(exc) and marker not in repr(exc)


@pytest.fixture
def token_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts every read of the access token's bytes."""
    reads: list[int] = []
    original = AccessToken.authorization_header

    def counted(self: AccessToken) -> str:
        reads.append(1)
        return original(self)

    monkeypatch.setattr(AccessToken, "authorization_header", counted)
    return reads


# --- the tool policy ------------------------------------------------------------

REFUSED = [
    ("submit_proposal", {"job": "~0212345", "cover_letter": "x"}, "tool_not_allowed"),
    ("send_message", {"action": "search"}, "tool_not_allowed"),
    ("search_contacts", {}, "tool_not_allowed"),
    ("find_jobs", {"action": "apply"}, "action_not_allowed"),
    ("find_jobs", {"query": "python"}, "action_not_allowed"),
    ("find_jobs", {"action": ["search"]}, "action_not_allowed"),
    ("find_jobs", {"action": None}, "action_not_allowed"),
    ("list_accounts", {"action": "get"}, "action_not_allowed"),
    ("list_accounts", None, "action_not_allowed"),
    ("find_jobs", [("action", "search")], "action_not_allowed"),
]


class Disguised(str):
    """A string that compares and hashes as ``looks_like`` but carries its own
    characters onto the wire."""

    looks_like: str | None = None

    def __eq__(self, other: object) -> bool:
        return other == self.looks_like

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash(self.looks_like)


def disguised(real: str, looks_like: str | None) -> str:
    cls = type("Disguised", (Disguised,), {"looks_like": looks_like})
    value: str = cls(real)
    return value


ALIASED = [
    (disguised("submit_proposal", "list_accounts"), {}, "tool_not_allowed"),
    (
        "find_jobs",
        {"action": disguised("delete", "get")},
        "action_not_allowed",
    ),
    ("list_accounts", {disguised("action", None): "get"}, "action_not_allowed"),
    (
        "find_jobs",
        {"action": "get", disguised("action", None): "delete"},
        "action_not_allowed",
    ),
]
REFUSED += ALIASED


@pytest.mark.parametrize(("name", "arguments", "code"), REFUSED)
def test_a_refused_tool_or_action_sends_nothing_and_reads_nothing(
    name: str, arguments: dict[str, object], code: str, token_reads: list[int]
) -> None:
    fake = FakeMcp()
    with fake.session() as session:
        ready(session)
        sent, lookups, reads, clock_reads = (
            len(fake.requests),
            len(fake.lookups),
            len(token_reads),
            fake.clock.reads,
        )
        with pytest.raises(ToolRefused) as caught:
            session.call_tool(name, arguments)
    assert_content_free(caught.value, code)
    assert len(fake.requests) == sent
    assert len(fake.lookups) == lookups
    assert len(token_reads) == reads
    assert fake.clock.reads == clock_reads


@pytest.mark.parametrize(("name", "arguments", "code"), REFUSED[:4])
def test_the_policy_is_checked_before_the_session_state(
    name: str, arguments: dict[str, object], code: str, token_reads: list[int]
) -> None:
    fake = FakeMcp()
    session = fake.session()
    with pytest.raises(ToolRefused) as caught:
        session.call_tool(name, arguments)
    assert caught.value.code == code
    with session, pytest.raises(ToolRefused):
        session.call_tool(name, arguments)
    assert fake.requests == []
    assert fake.lookups == []
    assert token_reads == []


def test_a_policy_is_frozen_and_required() -> None:
    assert dict(POLICY) == {
        "list_accounts": None,
        "find_jobs": frozenset({"smart_search", "search", "get"}),
    }
    with pytest.raises(TypeError):
        POLICY["find_jobs"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        ToolPolicy({"find_jobs": "search"})
    with pytest.raises(TypeError):
        McpSession(
            RESOURCE,
            AccessToken(ACCESS),
            {"list_accounts": None},  # type: ignore[arg-type]
            deadline=420.0,
        )


# --- one request per method ---------------------------------------------------


def test_each_method_sends_exactly_one_request_and_entry_sends_none() -> None:
    fake = FakeMcp()
    with fake.session() as session:
        assert fake.requests == []
        session.initialize()
        assert fake.methods() == ["initialize"]
        session.notify_initialized()
        assert fake.methods() == ["initialize", "notifications/initialized"]
        result = session.call_tool("list_accounts", {})
        assert len(fake.requests) == 3
        session.call_tool("find_jobs", {"action": "search", "query": "python"})
        session.call_tool("find_jobs", {"action": "get", "id": "~0212345"})
    assert len(fake.requests) == 5
    assert set(fake.methods()) <= PERMITTED
    calls = [_body(r)["params"] for r in fake.requests[2:]]
    assert calls == [
        {"name": "list_accounts", "arguments": {}},
        {"name": "find_jobs", "arguments": {"action": "search", "query": "python"}},
        {"name": "find_jobs", "arguments": {"action": "get", "id": "~0212345"}},
    ]
    assert [_body(r)["id"] for r in fake.requests[2:]] == [2, 3, 4]
    assert "id" not in _body(fake.requests[1])
    assert result == tool_result({"jobs": []})


@pytest.mark.parametrize(
    "steps",
    [
        ["call_tool"],
        ["notify"],
        ["initialize", "call_tool"],
        ["initialize", "initialize"],
        ["initialize", "notify", "notify"],
    ],
)
def test_out_of_order_calls_are_protocol_with_nothing_sent(steps: list[str]) -> None:
    fake = FakeMcp()
    actions: dict[str, Callable[[McpSession], object]] = {
        "initialize": lambda s: s.initialize(),
        "notify": lambda s: s.notify_initialized(),
        "call_tool": lambda s: s.call_tool("list_accounts", {}),
    }
    with fake.session() as session:
        for step in steps[:-1]:
            actions[step](session)
        sent = len(fake.requests)
        exc = mcp_failure(lambda: actions[steps[-1]](session))
    assert_content_free(exc, "protocol")
    assert len(fake.requests) == sent


def test_a_session_outside_its_context_sends_nothing() -> None:
    fake = FakeMcp()
    session = fake.session()
    assert_content_free(mcp_failure(session.initialize), "protocol")
    with session:
        ready(session)
    assert_content_free(
        mcp_failure(lambda: session.call_tool("list_accounts", {})), "protocol"
    )
    assert len(fake.requests) == 2


# --- wire format ----------------------------------------------------------------


def test_the_initialize_request_is_the_canonical_body_with_the_token() -> None:
    fake = FakeMcp()
    with fake.session() as session:
        ready(session)
        session.call_tool("list_accounts", {})
    first = fake.requests[0]
    assert first.content == egress.INITIALIZE_BODY
    assert first.method == "POST"
    for request in fake.requests:
        assert str(request.url) == RESOURCE
        assert request.headers["accept"] == "application/json, text/event-stream"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        assert request.headers["accept-encoding"] == "identity"
    assert "mcp-session-id" not in first.headers
    assert "mcp-protocol-version" not in first.headers
    for request in fake.requests[1:]:
        assert request.headers["mcp-session-id"] == "session-7f3a"
        assert request.headers["mcp-protocol-version"] == "2025-06-18"


def test_a_server_without_sessions_gets_no_session_header() -> None:
    fake = FakeMcp()
    fake.session_id = None
    with fake.session() as session:
        ready(session)
        session.call_tool("list_accounts", {})
    for request in fake.requests[1:]:
        assert "mcp-session-id" not in request.headers
        assert request.headers["mcp-protocol-version"] == "2025-06-18"


@pytest.mark.parametrize(
    "result",
    [
        {"capabilities": {}},
        {"protocolVersion": 20250618},
        {"protocolVersion": "2025 06 18"},
    ],
)
def test_an_initialize_result_without_a_usable_version_is_protocol(
    result: dict[str, object],
) -> None:
    fake = FakeMcp()
    fake.initialize = lambda r: httpx.Response(200, json=rpc(1, result))
    with fake.session() as session:
        assert_content_free(mcp_failure(session.initialize), "protocol")
        assert_content_free(mcp_failure(session.notify_initialized), "protocol")
    assert len(fake.requests) == 1


def test_an_unprintable_session_id_is_protocol() -> None:
    fake = FakeMcp()
    fake.session_id = "two words"
    with fake.session() as session:
        assert_content_free(mcp_failure(session.initialize), "protocol")


def test_an_event_stream_answer_is_read_up_to_the_matching_response() -> None:
    fake = FakeMcp()
    clock = fake.clock
    answer = sse_message(rpc(2, tool_result({"jobs": ["~0211111"]})))
    stream: list[httpx.SyncByteStream] = []

    def tool(request: httpx.Request) -> httpx.Response:
        keeps_open = Trickle(clock, 1.0)
        stream.append(keeps_open)
        body = (
            b": keep-alive\r\n\r\n"
            + b"id: 1\r\ndata:\r\n\r\n"
            + b"event: ping\r\ndata: {}\r\n\r\n"
            + sse_message({"jsonrpc": "2.0", "method": "notifications/progress"})
            + sse_message({"jsonrpc": "2.0", "id": 9, "method": "ping"})
            + sse_message(rpc(1, {"other": True}))
            + answer.replace(b"\n", b"\r")
        )
        # One byte per chunk, so every line end and field straddles chunks.
        return event_stream(
            *[body[i : i + 1] for i in range(len(body))], then=keeps_open
        )

    fake.tool = tool
    with fake.session() as session:
        ready(session)
        result = session.call_tool("find_jobs", {"action": "search"})
    assert result == tool_result({"jobs": ["~0211111"]})
    assert isinstance(stream[0], Trickle) and stream[0].sent <= 1


def test_an_event_stream_with_a_byte_order_mark_and_multiline_data_is_read() -> None:
    fake = FakeMcp()
    document = json.dumps(rpc(1, {"protocolVersion": "2025-06-18"}), indent=1)
    data = b"".join(b"data: " + line.encode() + b"\n" for line in document.split("\n"))
    fake.initialize = lambda r: event_stream(b"\xef\xbb", b"\xbf" + data + b"\n")
    with fake.session() as session:
        session.initialize()
        session.notify_initialized()


@pytest.mark.parametrize(
    "body",
    [
        b"data: {not json\n\n",
        b"data: \xff\xfe\n\n",
        sse_message({"jsonrpc": "2.0", "id": 2, "result": {}, "error": {}}),
        sse_message({"jsonrpc": "2.0", "id": 2, "error": {"code": -32602}}),
        sse_message({"jsonrpc": "2.0", "id": 2, "result": ["not", "an", "object"]}),
        sse_message(rpc(3, tool_result({}))),
        sse_message(rpc(True, tool_result({}))),
        sse_message(rpc(2, tool_result({}))).rstrip(b"\n"),
        b"",
    ],
    ids=[
        "bad-json",
        "bad-utf8",
        "result-and-error",
        "rpc-error",
        "result-not-object",
        "other-id-only",
        "boolean-id",
        "unterminated-event",
        "empty",
    ],
)
def test_a_malformed_or_unanswered_event_stream_is_protocol(body: bytes) -> None:
    fake = FakeMcp()
    fake.tool = lambda r: event_stream(body)
    with fake.session() as session:
        ready(session)
        exc = mcp_failure(lambda: session.call_tool("list_accounts", {}))
    assert_content_free(exc, "protocol")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b'{"jsonrpc":"2.0","id":2,"result":{'),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 3, "result": {}}),
        httpx.Response(200, json={"jsonrpc": "1.0", "id": 2, "result": {}}),
        httpx.Response(200, json=[rpc(2, {})]),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "error": {"code": 1}}),
        httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<p>"),
        httpx.Response(200),
    ],
    ids=[
        "truncated",
        "other-id",
        "wrong-version",
        "batch",
        "rpc-error",
        "html",
        "no-type",
    ],
)
def test_a_malformed_json_answer_is_protocol(response: httpx.Response) -> None:
    fake = FakeMcp()
    fake.tool = lambda r: response
    with fake.session() as session:
        ready(session)
        exc = mcp_failure(lambda: session.call_tool("list_accounts", {}))
    assert_content_free(exc, "protocol")


def test_the_notification_answer_is_not_read_past_its_first_chunk() -> None:
    fake = FakeMcp()
    keeps_open = Trickle(fake.clock, 1.0)
    fake.notify = lambda r: httpx.Response(202, stream=keeps_open)
    with fake.session() as session:
        ready(session)
    assert keeps_open.sent == 1


def test_unserialisable_arguments_are_protocol_with_nothing_sent() -> None:
    fake = FakeMcp()
    with fake.session() as session:
        ready(session)
        exc = mcp_failure(
            lambda: session.call_tool("find_jobs", {"action": "get", "n": float("nan")})
        )
    assert_content_free(exc, "protocol")
    assert len(fake.requests) == 2


# --- budgets and the deadline -------------------------------------------------


def test_a_trickling_answer_is_abandoned_at_the_first_chunk_after_50_s() -> None:
    fake = FakeMcp()
    stream: list[Trickle] = []

    def tool(request: httpx.Request) -> httpx.Response:
        stream.append(Trickle(fake.clock, 4.0))
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=stream[0]
        )

    fake.tool = tool
    with fake.session() as session:
        ready(session)
        start = fake.clock.now
        exc = mcp_failure(lambda: session.call_tool("list_accounts", {}))
    assert_content_free(exc, "unavailable")
    assert fake.clock.now - start == 52.0
    assert stream[0].sent == 13
    assert fake.clock.now - start <= 55.0


def test_a_stalled_answer_ends_on_the_5_s_read_timeout() -> None:
    fake = FakeMcp()

    class Stalls(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for _ in range(12):
                fake.clock.advance(4.0)
                yield b":\n"
            fake.clock.advance(5.0)  # the read timeout fires 5 s after 48 s
            raise httpx.ReadTimeout(ORG)

    fake.tool = lambda r: httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, stream=Stalls()
    )
    with fake.session() as session:
        ready(session)
        start = fake.clock.now
        exc = mcp_failure(lambda: session.call_tool("list_accounts", {}))
    assert_content_free(exc, "unavailable")
    assert fake.clock.now - start == 53.0
    for request in fake.requests:
        assert request.extensions["timeout"] == TIMEOUT_EXTENSION


def test_the_budget_is_clipped_to_the_deadline() -> None:
    fake = FakeMcp()
    fake.tool = lambda r: httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        stream=Trickle(fake.clock, 4.0),
    )
    with fake.session(deadline=30.0) as session:
        ready(session)
        exc = mcp_failure(lambda: session.call_tool("list_accounts", {}))
    assert_content_free(exc, "unavailable")
    assert fake.clock.now == 32.0


@pytest.mark.parametrize(("left", "sends"), [(4.9, False), (5.0, True), (60.0, True)])
def test_no_request_starts_with_under_5_s_left(left: float, sends: bool) -> None:
    fake = FakeMcp()
    with fake.session(deadline=left) as session:
        if sends:
            session.initialize()
        else:
            assert_content_free(mcp_failure(session.initialize), "unavailable")
    assert len(fake.requests) == (1 if sends else 0)


def test_every_method_respects_the_deadline() -> None:
    fake = FakeMcp()
    with fake.session(deadline=100.0) as session:
        session.initialize()
        fake.clock.now = 95.5
        assert_content_free(mcp_failure(session.notify_initialized), "unavailable")
        fake.clock.now = 10.0
        session.notify_initialized()
        fake.clock.now = 96.0
        exc = mcp_failure(lambda: session.call_tool("list_accounts", {}))
    assert_content_free(exc, "unavailable")
    assert len(fake.requests) == 2


def test_an_answer_over_4_mib_is_unavailable() -> None:
    fake = FakeMcp()
    big = b":" + b"x" * (4 * 1024 * 1024) + b"\n"
    fake.tool = lambda r: event_stream(big)
    with fake.session() as session:
        ready(session)
        exc = mcp_failure(lambda: session.call_tool("list_accounts", {}))
    assert_content_free(exc, "unavailable")


def test_an_answer_of_exactly_4_mib_is_read() -> None:
    fake = FakeMcp()
    answer = sse_message(rpc(2, tool_result({})))
    padding = b":" + b"x" * (4 * 1024 * 1024 - len(answer) - 2) + b"\n"
    fake.tool = lambda r: event_stream(padding, answer)
    with fake.session() as session:
        ready(session)
        assert session.call_tool("list_accounts", {}) == tool_result({})


# --- destinations ---------------------------------------------------------------


@pytest.mark.parametrize("address", NON_PUBLIC.values(), ids=NON_PUBLIC.keys())
def test_a_non_public_resource_is_refused_before_any_byte(address: str) -> None:
    fake = FakeMcp()
    fake.addresses["mcp.example.com"] = address
    with fake.session() as session:
        exc = mcp_failure(session.initialize)
    assert_content_free(exc, "endpoint_misconfigured")
    assert fake.requests == []


@pytest.mark.parametrize("address", NON_PUBLIC.values(), ids=NON_PUBLIC.keys())
def test_a_literal_non_public_resource_is_refused(address: str) -> None:
    fake = FakeMcp()
    host = f"[{address}]" if ":" in address else address
    with fake.session(url=f"https://{host}/mcp") as session:
        exc = mcp_failure(session.initialize)
    assert_content_free(exc, "endpoint_misconfigured")
    assert fake.requests == []


def test_a_host_turning_private_mid_session_gets_no_more_requests() -> None:
    fake = FakeMcp()
    with fake.session() as session:
        ready(session)
        fake.addresses["mcp.example.com"] = "10.0.0.8"
        exc = mcp_failure(lambda: session.call_tool("list_accounts", {}))
    assert_content_free(exc, "endpoint_misconfigured")
    assert len(fake.requests) == 2
    assert fake.lookups == ["mcp.example.com"] * 3


def test_a_resource_url_that_is_not_https_is_refused() -> None:
    fake = FakeMcp()
    with fake.session(url="http://mcp.example.com/mcp") as session:
        exc = mcp_failure(session.initialize)
    assert_content_free(exc, "endpoint_misconfigured")
    assert fake.requests == []


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_refused_and_the_token_goes_nowhere_else(status: int) -> None:
    fake = FakeMcp()
    fake.tool = lambda r: httpx.Response(
        status, headers={"Location": f"https://www.example.com/{ORG}"}
    )
    with fake.session() as session:
        ready(session)
        exc = mcp_failure(lambda: session.call_tool("list_accounts", {}))
    assert_content_free(exc, "endpoint_misconfigured")
    assert [str(r.url) for r in fake.requests] == [RESOURCE] * 3
    assert all(r.headers["authorization"] == f"Bearer {ACCESS}" for r in fake.requests)


# --- failure codes --------------------------------------------------------------


def _raises(exc: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("handler", "code", "retry_after"),
    [
        (lambda r: httpx.Response(401, text=ORG), "unauthorized", None),
        (lambda r: httpx.Response(403, text=ORG), "unauthorized", None),
        (
            lambda r: httpx.Response(429, headers={"Retry-After": "30"}),
            "rate_limited",
            30,
        ),
        (
            lambda r: httpx.Response(
                429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
            ),
            "rate_limited",
            None,
        ),
        (
            lambda r: httpx.Response(500, text=f"<html>{ORG}</html>"),
            "unavailable",
            None,
        ),
        (lambda r: httpx.Response(503), "unavailable", None),
        (lambda r: httpx.Response(400, json={"error": ORG}), "protocol", None),
        (lambda r: httpx.Response(404), "protocol", None),
        (_raises(httpx.ConnectError(ORG)), "unavailable", None),
        (_raises(httpx.ReadTimeout(ORG)), "unavailable", None),
        (lambda r: httpx.Response(200, content=b'{"jsonrpc":"2.0",'), "protocol", None),
    ],
    ids=[
        "401",
        "403",
        "429-seconds",
        "429-date",
        "500",
        "503",
        "400",
        "404",
        "network",
        "timeout",
        "malformed",
    ],
)
def test_failure_code_mapping(
    handler: Handler, code: str, retry_after: int | None
) -> None:
    fake = FakeMcp()
    fake.tool = handler
    with fake.session() as session:
        ready(session)
        exc = mcp_failure(lambda: session.call_tool("list_accounts", {}))
    assert_content_free(exc, code)
    assert exc.retry_after == retry_after
    assert exc.tool_code is None


@pytest.mark.parametrize(
    ("result", "tool_code"),
    [
        (
            tool_result(
                {"code": "SENSITIVE_RATE_LIMIT_EXCEEDED", "message": ORG}, is_error=True
            ),
            "SENSITIVE_RATE_LIMIT_EXCEEDED",
        ),
        (
            {
                "content": [
                    {"type": "image", "data": ORG},
                    {"type": "text", "text": f"failed for {ORG}"},
                    {"type": "text", "text": json.dumps({"code": "NOT_FOUND"})},
                ],
                "isError": True,
            },
            "NOT_FOUND",
        ),
        (tool_result({"message": ORG}, is_error=True), None),
        (tool_result({"code": ORG}, is_error=True), None),
        (tool_result({"code": 429}, is_error=True), None),
        ({"isError": True}, None),
        (
            tool_result(
                {"error_code": "SENSITIVE_RATE_LIMIT_EXCEEDED", "reason": ORG},
                is_error=True,
            ),
            "SENSITIVE_RATE_LIMIT_EXCEEDED",
        ),
        (
            tool_result(
                {"error_code": "RATE_LIMITED", "code": "NOT_FOUND"}, is_error=True
            ),
            "RATE_LIMITED",
        ),
        (
            tool_result({"error_code": ORG, "code": "NOT_FOUND"}, is_error=True),
            None,
        ),
        (
            tool_result({"error_code": None, "code": "NOT_FOUND"}, is_error=True),
            None,
        ),
    ],
    ids=[
        "code",
        "later-block",
        "no-code",
        "not-a-code",
        "number",
        "no-content",
        "error-code",
        "error-code-over-code",
        "invalid-error-code-no-fallback",
        "null-error-code-no-fallback",
    ],
)
def test_a_tool_error_exposes_only_its_short_code(
    result: dict[str, object], tool_code: str | None
) -> None:
    fake = FakeMcp()
    fake.tool = lambda r: httpx.Response(200, json=rpc(_body(r)["id"], result))
    with fake.session() as session:
        ready(session)
        exc = mcp_failure(lambda: session.call_tool("find_jobs", {"action": "get"}))
    assert_content_free(exc, "tool_error")
    assert exc.tool_code == tool_code


def test_a_result_that_is_not_an_error_is_returned_whole() -> None:
    fake = FakeMcp()
    result = {"content": [], "isError": "true", "structuredContent": {"n": 1}}
    fake.tool = lambda r: httpx.Response(200, json=rpc(_body(r)["id"], result))
    with fake.session() as session:
        ready(session)
        assert session.call_tool("list_accounts", {}) == result


# --- content-free failures (markers) -----------------------------------------


MARKED = [
    lambda r: httpx.Response(400, json={"error": ORG, "token": ACCESS}),
    lambda r: httpx.Response(
        200, content=f'{{"result":{{"org":"{ORG}","t":"{ACCESS}'.encode()
    ),
    lambda r: httpx.Response(500, text=f"<html>{ORG} {ACCESS}</html>"),
    lambda r: httpx.Response(
        200, headers={"Content-Type": f"application/{ORG}"}, content=ACCESS.encode()
    ),
    lambda r: httpx.Response(429, headers={"Retry-After": ORG}, text=ACCESS),
    lambda r: httpx.Response(401, headers={"WWW-Authenticate": f"Bearer {ACCESS}"}),
    lambda r: event_stream(f"data: {{{ORG} {ACCESS}\n\n".encode()),
    lambda r: httpx.Response(
        200,
        json=rpc(2, tool_result({"code": ORG, "message": ACCESS}, is_error=True)),
    ),
    lambda r: httpx.Response(302, headers={"Location": f"https://{ORG}.example.com"}),
    _raises(httpx.ConnectError(f"{ORG} {ACCESS}")),
]


@pytest.mark.parametrize("handler", MARKED)
def test_no_failure_carries_upstream_text(
    handler: Handler, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeMcp()
    fake.tool = handler
    with fake.session() as session:
        ready(session)
        exc = mcp_failure(lambda: session.call_tool("list_accounts", {}))
    assert_content_free(exc, exc.code)
    assert exc.tool_code is None
    for record in caplog.records:
        text = record.getMessage() + (record.exc_text or "")
        assert ORG not in text and ACCESS not in text


def test_every_failure_code_is_content_free() -> None:
    seen: set[str] = set()
    for handler in [*MARKED, lambda r: httpx.Response(403, text=ORG)]:
        fake = FakeMcp()
        fake.tool = handler
        with fake.session() as session:
            ready(session)
            exc = mcp_failure(lambda: session.call_tool("list_accounts", {}))
        assert_content_free(exc, exc.code)
        seen.add(exc.code)
    fake = FakeMcp()
    with fake.session(url=f"https://10.0.0.8/{ORG}") as session:
        exc = mcp_failure(session.initialize)
    assert_content_free(exc, "endpoint_misconfigured")
    seen.add(exc.code)
    assert seen == {
        "unauthorized",
        "rate_limited",
        "unavailable",
        "endpoint_misconfigured",
        "protocol",
        "tool_error",
    }
    assert repr(AccessToken(ACCESS)) == "AccessToken(<redacted>)"


# --- the wire trace -------------------------------------------------------------

TRACE_ENV = "RHEO__connectors__mcp_client__trace"
CURSOR = "cursor-marker-4e7b1d92-0c3a-4f86-a5d1-9b2e7c4f0a63"
BODY = "body-marker-2d9f6a13-8e4c-4b70-91f2-5a3c7e0d6b84"
SESSION = "session-marker-7c1e5b30-9a2d-4f64-8b07-3e6d1f9a2c55"
TRACE_MARKERS = (ACCESS, ORG, CURSOR, BODY, SESSION)
SEARCH = {"action": "search", "organization_id": ORG, "cursor": CURSOR, "q": BODY}


def traced(caplog: pytest.LogCaptureFixture) -> list[dict[str, str]]:
    """The trace lines, parsed, after checking each carries no marker."""
    lines: list[dict[str, str]] = []
    for record in caplog.records:
        formatted = logging.Formatter("%(message)s").format(record)
        text = formatted + (record.exc_text or "") + repr(record.args)
        for marker in TRACE_MARKERS:
            assert marker not in text
        if record.name != mcp_client.TRACE_LOGGER:
            continue
        assert "example" not in text  # no URL, not even the host
        assert record.levelno == logging.INFO
        message, error_text = record.getMessage().split(" error_text=", 1)
        head, *pairs = message.split(" ")
        assert head == "mcp_trace"
        line = dict(pair.split("=", 1) for pair in pairs)
        line["error_text"] = error_text
        lines.append(line)
    return lines


@pytest.fixture
def trace_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> pytest.LogCaptureFixture:
    monkeypatch.setenv(TRACE_ENV, "true")
    caplog.set_level(logging.DEBUG)
    return caplog


def marked_fake() -> FakeMcp:
    fake = FakeMcp()
    fake.session_id = SESSION
    return fake


def run_to_tool(fake: FakeMcp) -> McpFailure | None:
    with fake.session() as session:
        ready(session)
        try:
            session.call_tool("find_jobs", SEARCH)
        except McpFailure as failure:
            return failure
    return None


def test_the_trace_is_off_by_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(TRACE_ENV, raising=False)
    caplog.set_level(logging.DEBUG)
    fake = marked_fake()
    fake.tool = lambda r: httpx.Response(500, text=BODY)
    assert run_to_tool(fake) is not None
    with fake.session(deadline=1.0) as session:
        mcp_failure(session.initialize)
    assert traced(caplog) == []
    assert not [r for r in caplog.records if r.name == mcp_client.TRACE_LOGGER]


def test_a_successful_json_session_logs_one_line_per_request(
    trace_on: pytest.LogCaptureFixture,
) -> None:
    fake = marked_fake()
    fake.tool = lambda r: httpx.Response(
        200, json=rpc(_body(r)["id"], tool_result({"jobs": [BODY]}))
    )
    assert run_to_tool(fake) is None
    init, notify, tool = traced(trace_on)
    assert init == {
        "method": "initialize",
        "tool": "-",
        "action": "-",
        "status": "200",
        "egress": "-",
        "content_type": "application/json",
        "session_id": "true",
        "protocol": "2025-06-18",
        "rpc_error": "-",
        "is_error": "-",
        "tool_code": "-",
        "events": "-",
        "bytes": init["bytes"],
        "outcome": "ok",
        "error_text": "-",
    }
    assert int(init["bytes"]) > 0
    assert notify["method"] == "notifications/initialized"
    assert (notify["status"], notify["content_type"], notify["outcome"]) == (
        "202",
        "-",
        "ok",
    )
    assert (notify["session_id"], notify["bytes"], notify["protocol"]) == (
        "false",
        "0",
        "-",
    )
    assert (tool["method"], tool["tool"], tool["action"]) == (
        "tools/call",
        "find_jobs",
        "search",
    )
    assert (tool["status"], tool["is_error"], tool["outcome"]) == ("200", "false", "ok")


def test_a_tool_without_an_action_logs_none(trace_on: pytest.LogCaptureFixture) -> None:
    fake = marked_fake()
    with fake.session() as session:
        ready(session)
        session.call_tool("list_accounts", {})
    assert traced(trace_on)[-1]["action"] == "-"


def test_an_event_stream_answer_logs_its_event_and_byte_counts(
    trace_on: pytest.LogCaptureFixture,
) -> None:
    fake = marked_fake()
    progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
    chunks = (
        b": keepalive\n\n",
        sse_message(progress),
        sse_message(rpc(2, tool_result({"jobs": [BODY]}))),
    )
    fake.tool = lambda r: event_stream(*chunks)
    assert run_to_tool(fake) is None
    tool = traced(trace_on)[-1]
    assert tool["content_type"] == "text/event-stream"
    assert (tool["events"], tool["bytes"]) == ("2", str(sum(map(len, chunks))))
    assert tool["outcome"] == "ok"


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (401, "unauthorized"),
        (404, "protocol"),
        (429, "rate_limited"),
        (500, "unavailable"),
    ],
)
def test_a_failing_status_logs_the_status_and_the_mapped_code(
    trace_on: pytest.LogCaptureFixture, status: int, outcome: str
) -> None:
    fake = marked_fake()
    fake.tool = lambda r: httpx.Response(
        status,
        text=f"<html>{BODY} {ORG}</html>",
        headers={"Content-Type": "text/html; charset=utf-8", "Mcp-Session-Id": SESSION},
    )
    failure = run_to_tool(fake)
    assert failure is not None and failure.code == outcome
    assert_content_free(failure, outcome)
    tool = traced(trace_on)[-1]
    assert (tool["status"], tool["content_type"], tool["session_id"]) == (
        str(status),
        "text/html",
        "true",
    )
    assert (tool["egress"], tool["rpc_error"], tool["outcome"]) == ("-", "-", outcome)


@pytest.mark.parametrize("code", [-32601, -32602])
def test_a_json_rpc_error_on_initialize_logs_its_code(
    trace_on: pytest.LogCaptureFixture, code: int
) -> None:
    fake = marked_fake()
    error = {"code": code, "message": BODY, "data": {"org": ORG}}
    fake.initialize = lambda r: httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "error": error}
    )
    with fake.session() as session:
        assert_content_free(mcp_failure(session.initialize), "protocol")
    (init,) = traced(trace_on)
    assert (init["rpc_error"], init["protocol"], init["outcome"]) == (
        str(code),
        "-",
        "protocol",
    )


def test_a_json_rpc_error_on_a_tool_call_in_an_event_stream_logs_its_code(
    trace_on: pytest.LogCaptureFixture,
) -> None:
    fake = marked_fake()
    error = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": BODY}}
    fake.tool = lambda r: event_stream(sse_message(error))
    failure = run_to_tool(fake)
    assert failure is not None and failure.code == "protocol"
    tool = traced(trace_on)[-1]
    assert (tool["rpc_error"], tool["events"], tool["outcome"]) == (
        "-32602",
        "1",
        "protocol",
    )


def test_a_non_integer_json_rpc_error_code_is_not_logged(
    trace_on: pytest.LogCaptureFixture,
) -> None:
    fake = marked_fake()
    error = {"code": BODY, "message": BODY}
    fake.initialize = lambda r: httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "error": error}
    )
    with fake.session() as session:
        mcp_failure(session.initialize)
    assert traced(trace_on)[0]["rpc_error"] == "-"


def test_a_tool_error_result_logs_is_error_and_its_code(
    trace_on: pytest.LogCaptureFixture,
) -> None:
    fake = marked_fake()
    document = {"error_code": "ORG_NOT_FOUND", "message": BODY, "org": ORG}
    fake.tool = lambda r: httpx.Response(
        200, json=rpc(2, tool_result(document, is_error=True))
    )
    failure = run_to_tool(fake)
    assert failure is not None and failure.tool_code == "ORG_NOT_FOUND"
    tool = traced(trace_on)[-1]
    assert (tool["is_error"], tool["tool_code"], tool["outcome"]) == (
        "true",
        "ORG_NOT_FOUND",
        "tool_error",
    )


@pytest.mark.parametrize(
    ("result", "logged"),
    [
        ({"capabilities": {}}, "missing"),
        ({"protocolVersion": f"{BODY} 2025"}, "invalid"),
        ({"protocolVersion": 20250618}, "invalid"),
        ({"protocolVersion": "2025-03-26"}, "2025-03-26"),
    ],
    ids=["missing", "not-a-version", "not-a-string", "older"],
)
def test_initialize_logs_the_returned_protocol_version(
    trace_on: pytest.LogCaptureFixture, result: dict[str, object], logged: str
) -> None:
    fake = marked_fake()
    fake.initialize = lambda r: httpx.Response(200, json=rpc(1, result))
    with fake.session() as session:
        if logged in ("missing", "invalid"):
            mcp_failure(session.initialize)
        else:
            session.initialize()
    (init,) = traced(trace_on)
    assert init["protocol"] == logged
    assert init["outcome"] == ("protocol" if logged in ("missing", "invalid") else "ok")


@pytest.mark.parametrize(
    ("answer", "status", "content_type", "outcome"),
    [
        (lambda r: httpx.Response(202), "202", "-", "ok"),
        (
            lambda r: httpx.Response(200, json={"note": BODY}),
            "200",
            "application/json",
            "ok",
        ),
        (
            lambda r: httpx.Response(400, json={"error": BODY}),
            "400",
            "application/json",
            "protocol",
        ),
        (
            lambda r: httpx.Response(404, text="Not Found"),
            "404",
            "text/plain",
            "protocol",
        ),
    ],
    ids=["202", "200", "400", "404"],
)
def test_the_notification_logs_its_status(
    trace_on: pytest.LogCaptureFixture,
    answer: Handler,
    status: str,
    content_type: str,
    outcome: str,
) -> None:
    fake = marked_fake()
    fake.notify = answer
    with fake.session() as session:
        session.initialize()
        if outcome == "ok":
            session.notify_initialized()
        else:
            mcp_failure(session.notify_initialized)
    _, notify = traced(trace_on)
    assert notify["method"] == "notifications/initialized"
    assert (notify["status"], notify["content_type"], notify["outcome"]) == (
        status,
        content_type,
        outcome,
    )


def test_an_unusual_content_type_logs_as_other(
    trace_on: pytest.LogCaptureFixture,
) -> None:
    fake = marked_fake()
    fake.tool = lambda r: httpx.Response(
        200, content=BODY.encode(), headers={"Content-Type": f"{BODY} {ORG}"}
    )
    run_to_tool(fake)
    assert traced(trace_on)[-1]["content_type"] == "other"


@pytest.mark.parametrize(
    ("handler", "logged", "status", "outcome"),
    [
        (
            _raises(httpx.ConnectTimeout(ORG)),
            "timeout/connect_timeout",
            "-",
            "unavailable",
        ),
        (_raises(httpx.ReadTimeout(ORG)), "timeout/read_timeout", "-", "unavailable"),
        (
            _raises(httpx.ConnectError(ORG)),
            "unavailable/connect_error",
            "-",
            "unavailable",
        ),
        (
            _raises(httpx.RemoteProtocolError(ORG)),
            "unavailable/remote_protocol_error",
            "-",
            "unavailable",
        ),
        (
            lambda r: httpx.Response(
                302, headers={"Location": f"https://{ORG}.example.net"}
            ),
            "endpoint_misconfigured/redirect",
            "302",
            "endpoint_misconfigured",
        ),
        (
            lambda r: httpx.Response(
                200, content=b"x" * (mcp_client.RESPONSE_CAP_BYTES + 1)
            ),
            "too_large/too_large",
            "200",
            "unavailable",
        ),
    ],
    ids=[
        "connect-timeout",
        "read-timeout",
        "connect",
        "protocol-error",
        "redirect",
        "too-large",
    ],
)
def test_an_egress_failure_logs_its_code_and_detail(
    trace_on: pytest.LogCaptureFixture,
    handler: Handler,
    logged: str,
    status: str,
    outcome: str,
) -> None:
    fake = marked_fake()
    fake.tool = handler
    failure = run_to_tool(fake)
    assert failure is not None
    assert_content_free(failure, outcome)
    tool = traced(trace_on)[-1]
    assert (tool["egress"], tool["status"], tool["outcome"]) == (
        logged,
        status,
        outcome,
    )
    assert (tool["content_type"], tool["bytes"]) == ("-", "-")


def test_a_non_public_host_logs_before_anything_is_sent(
    trace_on: pytest.LogCaptureFixture,
) -> None:
    fake = marked_fake()
    fake.addresses["mcp.example.com"] = "10.1.2.3"
    with fake.session() as session:
        assert_content_free(mcp_failure(session.initialize), "endpoint_misconfigured")
    assert fake.requests == []
    (init,) = traced(trace_on)
    assert init["egress"] == "endpoint_misconfigured/not_public"


def test_a_request_with_too_little_time_left_logs_not_started(
    trace_on: pytest.LogCaptureFixture,
) -> None:
    fake = marked_fake()
    with fake.session(deadline=1.0) as session:
        mcp_failure(session.initialize)
    (init,) = traced(trace_on)
    assert (init["egress"], init["outcome"]) == ("not_started", "unavailable")


def test_refused_and_out_of_order_calls_log_nothing(
    trace_on: pytest.LogCaptureFixture,
) -> None:
    fake = marked_fake()
    with fake.session() as session:
        with pytest.raises(ToolRefused):
            session.call_tool("submit_proposal", {"cover_letter": BODY})
        mcp_failure(lambda: session.call_tool("find_jobs", SEARCH))
        mcp_failure(session.notify_initialized)
    assert traced(trace_on) == []


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_short_plain_text_4xx_records_its_text(
    trace_on: pytest.LogCaptureFixture, status: int
) -> None:
    fake = marked_fake()
    fake.tool = lambda r: httpx.Response(
        status,
        text="Bad request:\tunknown\nfield \x01 here",
        headers={"content-type": "text/plain; charset=utf-8"},
    )
    failure = run_to_tool(fake)
    assert failure is not None and failure.code == "protocol"
    *_, tool = traced(trace_on)
    assert tool["status"] == str(status)
    assert tool["error_text"] == '"Bad request: unknown field here"'


def test_a_long_plain_text_4xx_is_cut_to_the_limit(
    trace_on: pytest.LogCaptureFixture,
) -> None:
    fake = marked_fake()
    fake.tool = lambda r: httpx.Response(400, text="x" * 500)
    assert run_to_tool(fake) is not None
    *_, tool = traced(trace_on)
    assert tool["error_text"] == '"' + "x" * mcp_client.ERROR_TEXT_LIMIT + '"'


@pytest.mark.parametrize(
    "response",
    [
        lambda r: httpx.Response(400, json={"error": "nope"}),
        lambda r: httpx.Response(500, text="server broke"),
        lambda r: httpx.Response(
            400, text="data: x\n\n", headers={"content-type": "text/event-stream"}
        ),
    ],
    ids=["json-4xx", "plain-5xx", "event-stream-4xx"],
)
def test_other_error_bodies_are_never_recorded(
    trace_on: pytest.LogCaptureFixture,
    response: Callable[[httpx.Request], httpx.Response],
) -> None:
    fake = marked_fake()
    fake.tool = response
    assert run_to_tool(fake) is not None
    for line in traced(trace_on):
        assert line["error_text"] == "-"
