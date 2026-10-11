"""The one place for core's outbound HTTP rules (the OAuth client and the MCP client).

Surface (later callers read this module as built):

- :data:`Resolver`, :func:`default_resolver`: host name to address strings, on
  ``socket.getaddrinfo`` in production and injected in tests.
- :func:`check_public` ``(host, resolver, *, deadline, clock)``: refuses
  ``endpoint_misconfigured`` unless every address the host resolves to (or the
  literal IP host itself) is globally routable and not multicast. That refuses
  loopback, RFC 1918, link-local, unique-local, site-local (``fec0::/10``),
  unspecified and reserved addresses, and an IPv6 address that wraps an IPv4 one
  (IPv4-mapped, IPv4-compatible, NAT64 ``64:ff9b::/96``, 6to4) unless the wrapped
  address is public too (Python's registry already marks all of 6to4 ``2002::/16``
  non-global; the unwrap covers a runtime whose registry does not); a host that
  fails to resolve is refused the same way. The blocking resolver runs in a daemon
  thread bounded by the time left before ``deadline``, and the clock is checked
  again after it returns: either overrun is ``timeout``.
- :func:`open_client`: the ``httpx.Client`` both callers use: no redirects, the
  5 s timeout, and ``trust_env=False`` so neither an environment proxy nor a
  ``.netrc`` credential can change where a request goes or what it carries.
- :func:`bounded_request`: one streamed request. ``https`` only and a public host,
  both checked before any byte is sent; the ``budget`` starts before the host is
  resolved, so the lookup spends it too; ``httpx.Timeout(5.0)``; no redirects, and
  any 3xx is ``endpoint_misconfigured`` before a body is read; the injected
  monotonic clock is checked before the request, after resolving, after the
  headers and after every chunk (``timeout`` once spent, the response closed);
  ``Accept-Encoding: identity`` is always sent and bytes are counted on the same
  chunks against ``cap`` (``too_large``). A request therefore lasts at most
  ``budget`` plus one 5 s read. Any other status is returned as an
  :class:`EgressResponse` for the caller to judge.
- :class:`EgressFailure`: the fixed codes ``endpoint_misconfigured``, ``timeout``,
  ``too_large`` and ``unavailable`` (every transport or decoding error).
- :data:`INITIALIZE_BODY`: the canonical JSON-RPC 2.0 ``initialize`` request bytes,
  sent by the discovery probe and the MCP session alike.

Content-free failures: a failure's ``str`` and ``repr`` are its code alone, and it
is always raised after the ``except`` block that recorded the code has ended, so
both ``__cause__`` and ``__context__`` are ``None`` and no upstream text (a body,
a header, an exception message) can ride along into a log or a stored error.
Callers that convert an :class:`EgressFailure` follow the same rule.

The public-address check resolves the host itself and ``httpx`` resolves it again
when it connects; that DNS-rebinding window is accepted (the hosts are operator
configured or discovered from them over ``https``) and the check runs before every
request.
"""

import ipaddress
import json
import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

import httpx

Resolver = Callable[[str], Sequence[str]]
Clock = Callable[[], float]
EgressCode = Literal["endpoint_misconfigured", "timeout", "too_large", "unavailable"]

TIMEOUT: Final = httpx.Timeout(5.0)
"""Connect, read, write and pool, each 5 s: a stalled read ends 5 s after the last
chunk, which is what bounds a request to its budget plus one read."""

MCP_PROTOCOL_VERSION: Final = "2025-06-18"
CLIENT_NAME: Final = "Rheo Stream"
INITIALIZE_REQUEST_ID: Final = 1

INITIALIZE_BODY: Final[bytes] = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": INITIALIZE_REQUEST_ID,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "1"},
        },
    },
    separators=(",", ":"),
).encode("ascii")


class EgressFailure(Exception):
    """A refused or failed outbound request, carrying a fixed code only."""

    def __init__(self, code: EgressCode, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code: EgressCode = code
        self.retry_after = retry_after

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class EgressResponse:
    status: int
    headers: httpx.Headers
    body: bytes


def default_resolver(host: str) -> Sequence[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def _literal_address(host: str) -> str | None:
    candidate = host.removeprefix("[").removesuffix("]")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


_SITE_LOCAL: Final = ipaddress.ip_network("fec0::/10")
_IPV4_COMPATIBLE: Final = ipaddress.ip_network("::/96")
_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv4-mapped, IPv4-compatible, NAT64 or 6to4 address
    carries, else ``None``."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _IPV4_COMPATIBLE or ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.sixtofour


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if not ip.is_global or ip.is_multicast:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _SITE_LOCAL:
            return False
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return embedded.is_global and not embedded.is_multicast
    return True


def _resolve_within(
    host: str, resolver: Resolver, seconds: float
) -> list[str] | EgressCode:
    """Run the blocking ``resolver`` in a daemon thread for at most ``seconds``.

    Returns the addresses, ``"timeout"``, or ``"endpoint_misconfigured"`` when the
    lookup failed. An abandoned lookup keeps its thread until the resolver returns;
    it holds no lock and its result is discarded.
    """
    outcome: list[list[str] | EgressCode] = []

    def lookup() -> None:
        try:
            outcome.append(list(resolver(host)))
        except Exception:  # any resolver failure is a refusal
            outcome.append("endpoint_misconfigured")

    worker = threading.Thread(target=lookup, name="egress-resolve", daemon=True)
    worker.start()
    worker.join(max(seconds, 0.0))
    if not outcome:
        return "timeout"
    return outcome[0]


def check_public(
    host: str, resolver: Resolver, *, deadline: float, clock: Clock
) -> None:
    """Refuse unless ``host`` is public (see module), resolving it within the time
    left before ``deadline`` on ``clock``: ``timeout`` when the lookup outlasts it
    or the clock has passed it afterwards, ``endpoint_misconfigured`` otherwise."""
    code: EgressCode | None = None
    literal = _literal_address(host)
    if literal is not None:
        addresses: list[str] | EgressCode = [literal]
    else:
        addresses = _resolve_within(host, resolver, deadline - clock())
    if isinstance(addresses, str):
        code = addresses
    elif clock() >= deadline:
        code = "timeout"
    else:
        try:
            if not addresses or not all(_is_public(a) for a in addresses):
                code = "endpoint_misconfigured"
        except (TypeError, ValueError):  # an address that does not parse
            code = "endpoint_misconfigured"
    if code is not None:
        raise EgressFailure(code)


def open_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """The client every outbound request goes through (module docstring)."""
    return httpx.Client(
        transport=transport,
        timeout=TIMEOUT,
        follow_redirects=False,
        trust_env=False,
    )


def _https_host(url: str) -> str | None:
    """The host of an absolute ``https`` URL with a parseable port, else ``None``."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    return parts.hostname


def _stream(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    deadline: float,
    cap: int,
    clock: Clock,
    headers: httpx.Headers,
    content: bytes | None,
    data: Mapping[str, str] | None,
) -> EgressResponse | EgressCode:
    """Send and read under the budget; return the response or a failure code.

    It never raises :class:`EgressFailure` itself, so the caller's ``except`` for
    transport errors cannot swallow one.
    """
    with client.stream(
        method,
        url,
        headers=headers,
        content=content,
        data=data,
        timeout=TIMEOUT,
        follow_redirects=False,
    ) as response:
        if clock() >= deadline:
            return "timeout"
        if 300 <= response.status_code < 400:
            return "endpoint_misconfigured"
        body = bytearray()
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > cap:
                return "too_large"
            if clock() >= deadline:
                return "timeout"
        return EgressResponse(
            status=response.status_code,
            headers=httpx.Headers(response.headers),
            body=bytes(body),
        )


def bounded_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    budget: float,
    cap: int,
    resolver: Resolver,
    clock: Clock,
    headers: Mapping[str, str] | None = None,
    content: bytes | None = None,
    data: Mapping[str, str] | None = None,
) -> EgressResponse:
    """One bounded request (module docstring); raises :class:`EgressFailure`."""
    host = _https_host(url)
    if host is None:
        raise EgressFailure("endpoint_misconfigured")
    deadline = clock() + budget
    if budget <= 0:
        raise EgressFailure("timeout")
    check_public(host, resolver, deadline=deadline, clock=clock)
    sent_headers = httpx.Headers(headers)
    sent_headers["Accept-Encoding"] = "identity"
    outcome: EgressResponse | EgressCode
    try:
        outcome = _stream(
            client,
            method,
            url,
            deadline=deadline,
            cap=cap,
            clock=clock,
            headers=sent_headers,
            content=content,
            data=data,
        )
    except httpx.TimeoutException:
        outcome = "timeout"
    except Exception:  # every transport or decoding error becomes one fixed code
        outcome = "unavailable"
    if isinstance(outcome, EgressResponse):
        return outcome
    raise EgressFailure(outcome)
