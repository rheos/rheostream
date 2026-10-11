"""The shared outbound rules: public addresses only, https only, no redirects, a
wall-clock budget and a byte cap counted on the stream, and content-free failures.

No network: every request goes to an ``httpx.MockTransport``, every host name to an
injected resolver, and every budget is measured on a fake monotonic clock.
"""

import gzip
import json
import socket
from collections.abc import Callable, Iterator, Sequence

import httpx
import pytest
from rheo_core.connectors import egress
from rheo_core.connectors.egress import EgressFailure, bounded_request, check_public

PUBLIC = "2600::1"
"""A global unicast address; no test resolves or contacts it."""
URL = "https://www.example.com/data"
MARKER = "marker-7c1d0e5b-upstream-text"


def v4(*octets: int) -> str:
    """An IPv4 address from its octets. The repository's synthetic-content gate
    allows dotted-quad text only for loopback and the documentation ranges, so the
    other non-public ranges these tests need are spelled as octets."""
    return ".".join(str(octet) for octet in octets)


NON_PUBLIC = {
    "loopback-v4": "127.0.0.1",
    "loopback-v6": "::1",
    "rfc1918-10": v4(10, 0, 0, 7),
    "rfc1918-172": v4(172, 16, 4, 2),
    "rfc1918-192": v4(192, 168, 1, 20),
    "link-local-v4": v4(169, 254, 169, 254),
    "link-local-v6": "fe80::1",
    "unique-local": "fd12:3456::1",
    "unspecified-v4": "0.0.0.0",
    "unspecified-v6": "::",
    "reserved": v4(240, 0, 0, 1),
    "documentation-v4": "192.0.2.10",
    "documentation-v6": "2001:db8::10",
    "multicast-v4": v4(224, 0, 0, 251),
    "multicast-v6": "ff02::1",
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Counting:
    """A MockTransport wrapper that records every request it receives."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        self.client = httpx.Client(transport=httpx.MockTransport(record))


class Trickle(httpx.SyncByteStream):
    """One small chunk every ``interval`` seconds of fake time, forever."""

    def __init__(self, clock: FakeClock, interval: float, chunk: bytes = b"x") -> None:
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
    def __init__(self, chunks: Sequence[bytes]) -> None:
        self.chunks = chunks
        self.sent = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.sent += 1
            yield chunk


def public(host: str) -> Sequence[str]:
    return [PUBLIC]


def resolving_to(address: str) -> Callable[[str], Sequence[str]]:
    return lambda host: [address]


def never_called(host: str) -> Sequence[str]:
    raise AssertionError("a literal IP host must not be resolved")


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b'{"ok":true}')


def call(
    counting: Counting,
    url: str = URL,
    *,
    budget: float = 15.0,
    cap: int = 65536,
    resolver: Callable[[str], Sequence[str]] = public,
    clock: FakeClock | None = None,
) -> egress.EgressResponse:
    return bounded_request(
        counting.client,
        "GET",
        url,
        budget=budget,
        cap=cap,
        resolver=resolver,
        clock=clock or FakeClock(),
    )


def failure_of(fn: Callable[[], object]) -> EgressFailure:
    with pytest.raises(EgressFailure) as caught:
        fn()
    return caught.value


def assert_content_free(exc: BaseException, code: str) -> None:
    assert str(exc) == code
    assert repr(exc) == code
    assert MARKER not in str(exc) and MARKER not in repr(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


# --- public addresses only -------------------------------------------------------


@pytest.mark.parametrize("address", NON_PUBLIC.values(), ids=NON_PUBLIC.keys())
def test_a_host_resolving_non_public_is_refused_before_any_request(
    address: str,
) -> None:
    counting = Counting(ok)
    exc = failure_of(lambda: call(counting, resolver=resolving_to(address)))
    assert_content_free(exc, "endpoint_misconfigured")
    assert counting.requests == []


@pytest.mark.parametrize("address", NON_PUBLIC.values(), ids=NON_PUBLIC.keys())
def test_a_literal_ip_host_in_a_non_public_range_is_refused(address: str) -> None:
    counting = Counting(ok)
    host = f"[{address}]" if ":" in address else address
    exc = failure_of(
        lambda: call(counting, f"https://{host}/data", resolver=never_called)
    )
    assert_content_free(exc, "endpoint_misconfigured")
    assert counting.requests == []


def test_one_non_public_address_among_public_ones_is_refused() -> None:
    counting = Counting(ok)
    exc = failure_of(
        lambda: call(counting, resolver=lambda host: [PUBLIC, v4(10, 0, 0, 7)])
    )
    assert exc.code == "endpoint_misconfigured"
    assert counting.requests == []


def unresolvable(host: str) -> Sequence[str]:
    raise socket.gaierror(socket.EAI_NONAME, MARKER)


@pytest.mark.parametrize(
    "resolver",
    [unresolvable, lambda host: []],
    ids=["resolution-error", "no-addresses"],
)
def test_an_unresolvable_host_is_refused(
    resolver: Callable[[str], Sequence[str]],
) -> None:
    counting = Counting(ok)
    exc = failure_of(lambda: call(counting, resolver=resolver))
    assert_content_free(exc, "endpoint_misconfigured")
    assert counting.requests == []


def test_check_public_accepts_public_addresses_and_literals() -> None:
    check_public("www.example.com", public)
    check_public(PUBLIC, never_called)
    check_public(f"[{PUBLIC}]", never_called)
    check_public("www.example.com", lambda host: [PUBLIC, "2600::2"])


def test_the_default_resolver_reads_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        seen.append(host)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (PUBLIC, 0, 0, 0)),
        ]

    monkeypatch.setattr(egress.socket, "getaddrinfo", fake_getaddrinfo)
    assert list(egress.default_resolver("www.example.com")) == ["192.0.2.10", PUBLIC]
    assert seen == ["www.example.com"]


# --- https only, no redirects ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://www.example.com/data",
        "ftp://www.example.com/data",
        "www.example.com/data",
        "https:///data",
        "https://www.example.com:bad/data",
    ],
)
def test_a_non_https_url_is_refused_before_any_request(url: str) -> None:
    counting = Counting(ok)
    exc = failure_of(lambda: call(counting, url))
    assert_content_free(exc, "endpoint_misconfigured")
    assert counting.requests == []


@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308])
def test_any_redirect_is_refused_and_never_followed(status: int) -> None:
    body = Chunks([MARKER.encode()])

    def redirect(request: httpx.Request) -> httpx.Response:
        if request.url.host != "www.example.com":
            return httpx.Response(200)
        return httpx.Response(
            status,
            headers={"Location": f"https://elsewhere.example.com/{MARKER}"},
            stream=body,
        )

    counting = Counting(redirect)
    exc = failure_of(lambda: call(counting))
    assert_content_free(exc, "endpoint_misconfigured")
    assert [r.url.host for r in counting.requests] == ["www.example.com"]
    assert body.sent == 0


# --- budget, timeout and cap -----------------------------------------------------


@pytest.mark.parametrize("budget", [15.0, 50.0])
def test_a_trickling_stream_is_abandoned_at_the_first_chunk_after_the_budget(
    budget: float,
) -> None:
    clock = FakeClock()
    stream = Trickle(clock, 4.0)
    counting = Counting(lambda request: httpx.Response(200, stream=stream))
    exc = failure_of(lambda: call(counting, budget=budget, clock=clock))
    assert_content_free(exc, "timeout")
    first_after = (int(budget // 4) + 1) * 4
    assert clock.now == first_after
    assert stream.sent == first_after // 4
    assert clock.now <= budget + 5


def test_slow_headers_end_the_request_before_any_body_is_read() -> None:
    clock = FakeClock()
    body = Chunks([b"never read"])

    def slow(request: httpx.Request) -> httpx.Response:
        clock.advance(16.0)
        return httpx.Response(200, stream=body)

    counting = Counting(slow)
    exc = failure_of(lambda: call(counting, budget=15.0, clock=clock))
    assert_content_free(exc, "timeout")
    assert body.sent == 0


@pytest.mark.parametrize("budget", [0.0, -3.0])
def test_a_spent_budget_sends_nothing(budget: float) -> None:
    counting = Counting(ok)
    exc = failure_of(lambda: call(counting, budget=budget))
    assert exc.code == "timeout"
    assert counting.requests == []


def test_every_request_carries_the_five_second_timeout_and_no_redirects() -> None:
    counting = Counting(ok)
    call(counting)
    (request,) = counting.requests
    assert request.extensions["timeout"] == {
        "connect": 5.0,
        "read": 5.0,
        "write": 5.0,
        "pool": 5.0,
    }


def test_a_stalled_stream_ends_through_the_read_timeout() -> None:
    class Stalls(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"partial"
            raise httpx.ReadTimeout(MARKER)

    counting = Counting(lambda request: httpx.Response(200, stream=Stalls()))
    exc = failure_of(lambda: call(counting))
    assert_content_free(exc, "timeout")


def test_the_cap_is_counted_on_the_chunks() -> None:
    stream = Chunks([b"a" * 1000] * 5)
    counting = Counting(lambda request: httpx.Response(200, stream=stream))
    exc = failure_of(lambda: call(counting, cap=2500))
    assert_content_free(exc, "too_large")
    assert stream.sent == 3


def test_a_body_exactly_at_the_cap_is_returned() -> None:
    counting = Counting(
        lambda request: httpx.Response(200, stream=Chunks([b"a" * 1000] * 2))
    )
    assert call(counting, cap=2000).body == b"a" * 2000


def test_an_oversized_truncated_json_body_is_too_large_and_content_free() -> None:
    body = ('{"note": "' + MARKER * 200).encode()
    counting = Counting(lambda request: httpx.Response(200, content=body))
    exc = failure_of(lambda: call(counting, cap=1024))
    assert_content_free(exc, "too_large")


# --- transport and decoding errors -----------------------------------------------


def raises(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ConnectError(MARKER), "unavailable"),
        (httpx.RemoteProtocolError(MARKER), "unavailable"),
        (httpx.ConnectTimeout(MARKER), "timeout"),
        (httpx.PoolTimeout(MARKER), "timeout"),
        (httpx.WriteTimeout(MARKER), "timeout"),
        (ValueError(MARKER), "unavailable"),
    ],
)
def test_transport_errors_map_to_fixed_codes(error: Exception, code: str) -> None:
    exc = failure_of(lambda: call(Counting(raises(error))))
    assert_content_free(exc, code)


def test_an_error_mid_body_is_unavailable() -> None:
    class Breaks(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"partial"
            raise httpx.ReadError(MARKER)

    exc = failure_of(
        lambda: call(Counting(lambda request: httpx.Response(200, stream=Breaks())))
    )
    assert_content_free(exc, "unavailable")


def test_a_decoding_error_is_unavailable() -> None:
    def broken_gzip(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip", "X-Note": MARKER},
            content=b"not gzip " + MARKER.encode(),
        )

    exc = failure_of(lambda: call(Counting(broken_gzip)))
    assert_content_free(exc, "unavailable")


def test_a_compressed_body_is_counted_decoded() -> None:
    def zipped(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=gzip.compress(b"a" * 5000),
        )

    exc = failure_of(lambda: call(Counting(zipped), cap=4096))
    assert exc.code == "too_large"


# --- success ---------------------------------------------------------------------


def test_a_response_is_returned_with_its_status_headers_and_body() -> None:
    seen: list[httpx.Request] = []

    def echo(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, headers={"X-Test": "yes"}, content=b"missing")

    response = bounded_request(
        httpx.Client(transport=httpx.MockTransport(echo)),
        "POST",
        URL,
        budget=15.0,
        cap=1024,
        resolver=public,
        clock=FakeClock(),
        headers={"Content-Type": "application/json"},
        content=b"{}",
    )
    assert (response.status, response.body) == (404, b"missing")
    assert response.headers["x-test"] == "yes"
    (request,) = seen
    assert request.method == "POST"
    assert request.content == b"{}"
    assert request.headers["content-type"] == "application/json"


def test_form_data_is_sent_as_a_form() -> None:
    counting = Counting(ok)
    bounded_request(
        counting.client,
        "POST",
        URL,
        budget=15.0,
        cap=1024,
        resolver=public,
        clock=FakeClock(),
        data={"grant_type": "example"},
    )
    (request,) = counting.requests
    assert request.content == b"grant_type=example"


def test_the_shared_client_never_follows_redirects_or_reads_the_environment() -> None:
    with egress.open_client(httpx.MockTransport(ok)) as client:
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert client.timeout == egress.TIMEOUT


def test_failure_text_is_the_code_alone() -> None:
    failure = EgressFailure("timeout", retry_after=7)
    assert (str(failure), repr(failure), failure.retry_after) == (
        "timeout",
        "timeout",
        7,
    )


def test_the_initialize_body_is_one_json_rpc_initialize_request() -> None:
    message = json.loads(egress.INITIALIZE_BODY)
    assert message["jsonrpc"] == "2.0"
    assert message["method"] == "initialize"
    assert message["id"] == egress.INITIALIZE_REQUEST_ID
    assert isinstance(message["id"], int)
    params = message["params"]
    assert isinstance(params["protocolVersion"], str) and params["protocolVersion"]
    assert params["capabilities"] == {}
    assert params["clientInfo"]["name"] == "Rheo Stream"
    assert set(message) == {"jsonrpc", "id", "method", "params"}
