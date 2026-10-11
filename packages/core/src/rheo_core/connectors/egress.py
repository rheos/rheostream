"""The one place for core's outbound HTTP rules (the OAuth client and the MCP client).

Surface (later callers read this module as built):

- :data:`Resolver`, :func:`default_resolver`: host name to address strings, on
  ``socket.getaddrinfo`` in production and injected in tests.
- :func:`check_public`: refuses ``endpoint_misconfigured`` unless every address the
  host resolves to (or the literal IP host itself) is globally routable and not
  multicast. That refuses loopback, RFC 1918, link-local, unique-local, unspecified
  and reserved addresses; a host that fails to resolve is refused the same way.
- :func:`open_client`: the ``httpx.Client`` both callers use: no redirects, the
  5 s timeout, and ``trust_env=False`` so neither an environment proxy nor a
  ``.netrc`` credential can change where a request goes or what it carries.
- :func:`bounded_request`: one streamed request. ``https`` only and a public host,
  both checked before any byte is sent; ``httpx.Timeout(5.0)``; no redirects, and
  any 3xx is ``endpoint_misconfigured`` before a body is read; the injected
  monotonic clock is checked before the request, after the headers and after every
  chunk against ``budget`` seconds (``timeout`` once spent, the response closed);
  bytes are counted on the same chunks against ``cap`` (``too_large``). A request
  therefore lasts at most ``budget`` plus one 5 s read. Any other status is
  returned as an :class:`EgressResponse` for the caller to judge.
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


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast


def check_public(host: str, resolver: Resolver) -> None:
    """Refuse ``endpoint_misconfigured`` unless ``host`` is public (see module)."""
    refused = True
    try:
        literal = _literal_address(host)
        addresses = [literal] if literal is not None else list(resolver(host))
        refused = not addresses or not all(_is_public(a) for a in addresses)
    except Exception:  # any resolver or parse failure is a refusal
        refused = True
    if refused:
        raise EgressFailure("endpoint_misconfigured")


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
    headers: Mapping[str, str] | None,
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
    check_public(host, resolver)
    deadline = clock() + budget
    if clock() >= deadline:
        raise EgressFailure("timeout")
    outcome: EgressResponse | EgressCode
    try:
        outcome = _stream(
            client,
            method,
            url,
            deadline=deadline,
            cap=cap,
            clock=clock,
            headers=headers,
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
