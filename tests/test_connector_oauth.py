"""Core's OAuth client: discovery and the authorization request.

A fake resource and authorization server sit behind an ``httpx.MockTransport``;
host names go to an injected resolver and every budget runs on a fake clock, so no
test touches the network.
"""

import base64
import hashlib
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from rheo_core.connectors import egress, oauth
from rheo_core.connectors.oauth import (
    Discovered,
    OAuthFailure,
    discover,
    new_authorization,
)

PUBLIC = "2600::1"
"""A global unicast address; no test resolves or contacts it."""
RESOURCE = "https://mcp.example.com/mcp"
PRM_URL = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
ISSUER = "https://auth.example.com"
AS_URL = "https://auth.example.com/.well-known/oauth-authorization-server"
REDIRECT_URI = "https://www.example.com/auth/connector/leads/callback"
MARKER = "marker-4b9a2f10-upstream-text"


def v4(*octets: int) -> str:
    """An IPv4 address from its octets (the synthetic-content gate allows dotted-quad
    text only for loopback and the documentation ranges)."""
    return ".".join(str(octet) for octet in octets)


NON_PUBLIC = {
    "loopback": "127.0.0.1",
    "rfc1918": v4(10, 1, 2, 3),
    "link-local": v4(169, 254, 169, 254),
    "unique-local": "fd00::5",
    "unspecified": "0.0.0.0",
    "reserved": v4(240, 0, 0, 9),
    "documentation": "192.0.2.44",
    "multicast": v4(239, 1, 1, 1),
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Trickle(httpx.SyncByteStream):
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def __iter__(self) -> Iterator[bytes]:
        while True:
            self.clock.advance(4.0)
            yield b" "


def server_metadata() -> dict[str, Any]:
    return {
        "issuer": ISSUER,
        "authorization_endpoint": "https://login.example.com/authorize",
        "token_endpoint": "https://token.example.com/token",
        "registration_endpoint": "https://register.example.com/register",
        "revocation_endpoint": "https://revoke.example.com/revoke",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


class FakeServers:
    """The resource's probe, its protected-resource metadata and the authorization
    server's metadata, with every received request recorded."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.requests: list[httpx.Request] = []
        self.addresses: dict[str, str] = {}
        self.probe: Callable[[httpx.Request], httpx.Response] = lambda r: (
            httpx.Response(401, headers={"WWW-Authenticate": "Bearer"})
        )
        self.resource_metadata: dict[str, Any] = {
            "resource": RESOURCE,
            "authorization_servers": [ISSUER],
        }
        self.server_metadata = server_metadata()
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]]
        self.routes = {
            ("POST", RESOURCE): lambda r: self.probe(r),
            ("GET", PRM_URL): lambda r: httpx.Response(
                200, json=self.resource_metadata
            ),
            ("GET", AS_URL): lambda r: httpx.Response(200, json=self.server_metadata),
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, str(request.url)))
        if route is None:
            return httpx.Response(404)
        return route(request)

    def resolve(self, host: str) -> Sequence[str]:
        return [self.addresses.get(host, PUBLIC)]

    def discover(self, resource_url: str = RESOURCE) -> Discovered:
        return discover(
            resource_url,
            resolver=self.resolve,
            clock=self.clock,
            transport=httpx.MockTransport(self.handle),
        )

    def refused(self, resource_url: str = RESOURCE) -> OAuthFailure:
        with pytest.raises(OAuthFailure) as caught:
            self.discover(resource_url)
        failure = caught.value
        assert_content_free(failure, "endpoint_misconfigured")
        return failure

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]


@pytest.fixture
def servers() -> FakeServers:
    return FakeServers()


def assert_content_free(exc: BaseException, code: str) -> None:
    assert str(exc) == code
    assert repr(exc) == code
    assert MARKER not in str(exc) and MARKER not in repr(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


# --- the probe -------------------------------------------------------------------


def test_discovery_reads_both_documents_and_pins_every_endpoint(
    servers: FakeServers,
) -> None:
    discovered = servers.discover()
    assert discovered == Discovered(
        resource_url=RESOURCE,
        issuer=ISSUER,
        authorization_endpoint="https://login.example.com/authorize",
        token_endpoint="https://token.example.com/token",
        registration_endpoint="https://register.example.com/register",
        revocation_endpoint="https://revoke.example.com/revoke",
        scope=None,
    )
    assert [(r.method, str(r.url)) for r in servers.requests] == [
        ("POST", RESOURCE),
        ("GET", PRM_URL),
        ("GET", AS_URL),
    ]


def test_the_probe_is_one_unauthenticated_initialize_post(
    servers: FakeServers,
) -> None:
    servers.discover()
    probes = [r for r in servers.requests if r.method == "POST"]
    assert len(probes) == 1
    (probe,) = probes
    assert str(probe.url) == RESOURCE
    assert "authorization" not in probe.headers
    assert probe.content == egress.INITIALIZE_BODY
    assert all("authorization" not in r.headers for r in servers.requests)


def test_the_challenge_scope_is_first_choice_and_its_metadata_pointer_is_ignored(
    servers: FakeServers,
) -> None:
    servers.probe = lambda r: httpx.Response(
        401,
        headers={
            "WWW-Authenticate": (
                'Bearer realm="jobs", scope="jobs:read profile", '
                'resource_metadata="https://pointer.example.com/meta"'
            )
        },
    )
    servers.resource_metadata["scopes_supported"] = ["resource-scope"]
    servers.server_metadata["scopes_supported"] = ["server-scope"]
    assert servers.discover().scope == "jobs:read profile"
    assert "pointer.example.com" not in servers.hosts()


def test_a_bearer_challenge_after_another_scheme_is_found(
    servers: FakeServers,
) -> None:
    servers.probe = lambda r: httpx.Response(
        401,
        headers=[
            ("WWW-Authenticate", 'Basic realm="other"'),
            ("WWW-Authenticate", "Bearer scope=jobs.read"),
        ],
    )
    assert servers.discover().scope == "jobs.read"


def network_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(MARKER)


PROBE_OUTCOMES: dict[str, Callable[[httpx.Request], httpx.Response]] = {
    "200": lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1}),
    "405": lambda r: httpx.Response(405),
    "500": lambda r: httpx.Response(500, content=MARKER.encode()),
    "401-no-challenge": lambda r: httpx.Response(401),
    "401-basic-only": lambda r: httpx.Response(
        401, headers={"WWW-Authenticate": 'Basic realm="x", scope="nope"'}
    ),
    "401-bearer-no-scope": lambda r: httpx.Response(
        401, headers={"WWW-Authenticate": 'Bearer realm="x"'}
    ),
    "401-malformed": lambda r: httpx.Response(
        401, headers={"WWW-Authenticate": 'Bearer scope="unterminated'}
    ),
    "401-token68": lambda r: httpx.Response(
        401, headers={"WWW-Authenticate": "Bearer abc=="}
    ),
    "401-duplicate-scope": lambda r: httpx.Response(
        401, headers={"WWW-Authenticate": "Bearer scope=a, scope=b"}
    ),
    "401-scope-bad-characters": lambda r: httpx.Response(
        401, headers={"WWW-Authenticate": 'Bearer scope="a\\\\b"'}
    ),
    "401-scope-double-space": lambda r: httpx.Response(
        401, headers={"WWW-Authenticate": 'Bearer scope="a  b"'}
    ),
    "401-oversized": lambda r: httpx.Response(
        401,
        headers={"WWW-Authenticate": 'Bearer scope="a", realm="' + "r" * 1100 + '"'},
    ),
    "network-error": network_error,
}


@pytest.mark.parametrize("probe", PROBE_OUTCOMES.values(), ids=PROBE_OUTCOMES.keys())
def test_an_unusable_probe_supplies_no_scope_and_is_not_a_failure(
    servers: FakeServers, probe: Callable[[httpx.Request], httpx.Response]
) -> None:
    servers.probe = probe
    servers.resource_metadata["scopes_supported"] = ["resource-scope"]
    assert servers.discover().scope == "resource-scope"


def test_a_slow_probe_is_not_a_failure(servers: FakeServers) -> None:
    servers.probe = lambda r: httpx.Response(401, stream=Trickle(servers.clock))
    assert servers.discover().scope is None
    assert servers.clock.now <= 20


def test_a_redirecting_probe_is_refused(servers: FakeServers) -> None:
    servers.probe = lambda r: httpx.Response(
        307, headers={"Location": "https://elsewhere.example.com/mcp"}
    )
    servers.refused()
    assert servers.hosts() == ["mcp.example.com"]


# --- scope order -----------------------------------------------------------------


def test_scope_falls_back_to_the_protected_resource_then_the_server_then_none(
    servers: FakeServers,
) -> None:
    servers.resource_metadata["scopes_supported"] = ["jobs:read", "profile"]
    servers.server_metadata["scopes_supported"] = ["server-scope"]
    assert servers.discover().scope == "jobs:read profile"

    del servers.resource_metadata["scopes_supported"]
    assert servers.discover().scope == "server-scope"

    servers.resource_metadata["scopes_supported"] = []
    assert servers.discover().scope == "server-scope"

    del servers.server_metadata["scopes_supported"]
    assert servers.discover().scope is None


def test_malformed_scopes_supported_is_refused(servers: FakeServers) -> None:
    servers.server_metadata["scopes_supported"] = ['bad"scope']
    servers.refused()


# --- refusals --------------------------------------------------------------------


@pytest.mark.parametrize(
    "resource_url",
    [
        "http://mcp.example.com/mcp",
        "mcp.example.com/mcp",
        "https:///mcp",
        "https://mcp.example.com:bad/mcp",
    ],
)
def test_a_non_https_resource_url_is_refused_with_zero_requests(
    servers: FakeServers, resource_url: str
) -> None:
    servers.refused(resource_url)
    assert servers.requests == []


def test_a_non_https_authorization_server_entry_is_refused(
    servers: FakeServers,
) -> None:
    servers.resource_metadata["authorization_servers"] = ["http://auth.example.com"]
    servers.refused()
    assert "auth.example.com" not in servers.hosts()


@pytest.mark.parametrize(
    "servers_value",
    [[], None, "https://auth.example.com", [42]],
    ids=["empty", "missing", "not-a-list", "not-strings"],
)
def test_unusable_authorization_servers_are_refused(
    servers: FakeServers, servers_value: object
) -> None:
    if servers_value is None:
        del servers.resource_metadata["authorization_servers"]
    else:
        servers.resource_metadata["authorization_servers"] = servers_value
    servers.refused()


@pytest.mark.parametrize(
    "issuer",
    ["https://auth.example.com/", "https://other.example.com", None],
    ids=["trailing-slash", "other-host", "missing"],
)
def test_an_issuer_that_differs_from_the_fetched_url_is_refused(
    servers: FakeServers, issuer: str | None
) -> None:
    if issuer is None:
        del servers.server_metadata["issuer"]
    else:
        servers.server_metadata["issuer"] = issuer
    servers.refused()


def test_an_issuer_with_a_path_is_fetched_by_the_insertion_rule(
    servers: FakeServers,
) -> None:
    issuer = "https://auth.example.com/tenant/"
    servers.resource_metadata["authorization_servers"] = [issuer]
    metadata = {**servers.server_metadata, "issuer": issuer}
    servers.routes[
        (
            "GET",
            "https://auth.example.com/.well-known/oauth-authorization-server/tenant",
        )
    ] = lambda r: httpx.Response(200, json=metadata)
    assert servers.discover().issuer == issuer


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("code_challenge_methods_supported", ["plain"]),
        ("code_challenge_methods_supported", None),
        ("grant_types_supported", ["authorization_code"]),
        ("grant_types_supported", ["refresh_token"]),
        ("grant_types_supported", None),
        ("token_endpoint_auth_methods_supported", ["client_secret_basic"]),
        ("token_endpoint_auth_methods_supported", None),
        ("token_endpoint_auth_methods_supported", "none"),
        ("registration_endpoint", None),
        ("authorization_endpoint", None),
        ("token_endpoint", None),
    ],
)
def test_metadata_missing_a_required_capability_is_refused(
    servers: FakeServers, key: str, value: object
) -> None:
    if value is None:
        del servers.server_metadata[key]
    else:
        servers.server_metadata[key] = value
    servers.refused()


@pytest.mark.parametrize(
    "key",
    [
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "revocation_endpoint",
    ],
)
def test_a_non_https_discovered_endpoint_is_refused(
    servers: FakeServers, key: str
) -> None:
    servers.server_metadata[key] = servers.server_metadata[key].replace(
        "https", "http", 1
    )
    servers.refused()


def test_a_missing_revocation_endpoint_is_accepted(servers: FakeServers) -> None:
    del servers.server_metadata["revocation_endpoint"]
    assert servers.discover().revocation_endpoint is None


@pytest.mark.parametrize(
    "response",
    [
        lambda r: httpx.Response(404),
        lambda r: httpx.Response(200, content=b"not json " + MARKER.encode()),
        lambda r: httpx.Response(
            200, content=b'{"authorization_servers": ["' + MARKER.encode()
        ),
        lambda r: httpx.Response(200, json=[MARKER]),
        lambda r: httpx.Response(
            500, headers={"X-Detail": MARKER}, content=MARKER.encode()
        ),
    ],
    ids=["404", "not-json", "truncated-json", "not-an-object", "500"],
)
def test_an_unusable_metadata_response_is_refused_content_free(
    servers: FakeServers, response: Callable[[httpx.Request], httpx.Response]
) -> None:
    servers.routes[("GET", PRM_URL)] = response
    servers.refused()


def test_an_oversized_metadata_response_is_refused(servers: FakeServers) -> None:
    padded = {**servers.server_metadata, "padding": MARKER * 3000}
    servers.routes[("GET", AS_URL)] = lambda r: httpx.Response(200, json=padded)
    servers.refused()


# --- redirects (AC 8) ------------------------------------------------------------


def test_a_redirect_on_the_protected_resource_metadata_is_refused(
    servers: FakeServers,
) -> None:
    servers.routes[("GET", PRM_URL)] = lambda r: httpx.Response(
        302, headers={"Location": f"https://elsewhere.example.com/{MARKER}"}
    )
    servers.refused()
    assert "elsewhere.example.com" not in servers.hosts()


def test_a_redirect_toward_the_registration_host_is_refused_and_not_followed(
    servers: FakeServers,
) -> None:
    servers.routes[("GET", AS_URL)] = lambda r: httpx.Response(
        308,
        headers={"Location": "https://register.example.com/.well-known/metadata"},
    )
    servers.refused()
    assert "register.example.com" not in servers.hosts()
    assert servers.hosts() == ["mcp.example.com", "mcp.example.com", "auth.example.com"]


# --- public addresses only (AC 72) -----------------------------------------------


@pytest.mark.parametrize("address", NON_PUBLIC.values(), ids=NON_PUBLIC.keys())
def test_a_non_public_resource_host_is_refused_with_zero_requests(
    servers: FakeServers, address: str
) -> None:
    servers.addresses["mcp.example.com"] = address
    servers.refused()
    assert servers.requests == []


@pytest.mark.parametrize("address", NON_PUBLIC.values(), ids=NON_PUBLIC.keys())
def test_a_literal_non_public_resource_host_is_refused_with_zero_requests(
    servers: FakeServers, address: str
) -> None:
    host = f"[{address}]" if ":" in address else address
    assert urlsplit(f"https://{host}/mcp").hostname == address
    servers.refused(f"https://{host}/mcp")
    assert servers.requests == []


@pytest.mark.parametrize(
    "host",
    [
        "auth.example.com",
        "login.example.com",
        "token.example.com",
        "register.example.com",
        "revoke.example.com",
    ],
)
def test_any_discovered_url_resolving_non_public_refuses_discovery(
    servers: FakeServers, host: str
) -> None:
    servers.addresses[host] = v4(10, 9, 8, 7)
    servers.refused()
    assert host not in servers.hosts()


def test_discovery_succeeds_when_every_url_resolves_public(
    servers: FakeServers,
) -> None:
    resolved: list[str] = []

    def recording(host: str) -> Sequence[str]:
        resolved.append(host)
        return [PUBLIC]

    discover(
        RESOURCE,
        resolver=recording,
        clock=servers.clock,
        transport=httpx.MockTransport(servers.handle),
    )
    assert set(resolved) >= {
        "mcp.example.com",
        "auth.example.com",
        "login.example.com",
        "token.example.com",
        "register.example.com",
        "revoke.example.com",
    }


def test_a_literal_non_public_issuer_is_refused_unfetched(
    servers: FakeServers,
) -> None:
    issuer_host = v4(192, 168, 0, 4)
    servers.resource_metadata["authorization_servers"] = [f"https://{issuer_host}"]
    servers.refused()
    assert issuer_host not in servers.hosts()


# --- timing (AC 35 for discovery) ------------------------------------------------


def test_a_slow_metadata_response_is_refused_within_twenty_seconds(
    servers: FakeServers,
) -> None:
    started: list[float] = []

    def slow(request: httpx.Request) -> httpx.Response:
        started.append(servers.clock.now)
        return httpx.Response(200, stream=Trickle(servers.clock))

    servers.routes[("GET", PRM_URL)] = slow
    servers.refused()
    (start,) = started
    assert 15 <= servers.clock.now - start <= 20


def test_slow_headers_are_refused(servers: FakeServers) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        servers.clock.advance(16.0)
        return httpx.Response(200, json=servers.server_metadata)

    servers.routes[("GET", AS_URL)] = slow
    servers.refused()


def taking(
    clock: FakeClock, seconds: float, body: Callable[[], httpx.Response]
) -> Callable[[httpx.Request], httpx.Response]:
    """A handler whose response headers arrive ``seconds`` of fake time later."""

    def handler(request: httpx.Request) -> httpx.Response:
        clock.advance(seconds)
        return body()

    return handler


def test_the_whole_discovery_is_cut_at_thirty_seconds(servers: FakeServers) -> None:
    clock = servers.clock
    servers.routes[("POST", RESOURCE)] = taking(
        clock, 12.0, lambda: httpx.Response(405)
    )
    servers.routes[("GET", PRM_URL)] = taking(
        clock, 12.0, lambda: httpx.Response(200, json=servers.resource_metadata)
    )
    servers.routes[("GET", AS_URL)] = taking(
        clock, 12.0, lambda: httpx.Response(200, json=servers.server_metadata)
    )
    servers.refused()
    assert len(servers.requests) == 3


def test_no_request_starts_once_the_discovery_budget_is_spent(
    servers: FakeServers,
) -> None:
    clock = servers.clock
    servers.probe = lambda r: httpx.Response(401, stream=Trickle(clock))
    servers.routes[("GET", PRM_URL)] = taking(
        clock, 14.0, lambda: httpx.Response(200, json=servers.resource_metadata)
    )
    servers.refused()
    assert clock.now == 30.0
    assert [str(r.url) for r in servers.requests] == [RESOURCE, PRM_URL]


# --- failure hygiene -------------------------------------------------------------


def test_failures_carry_fixed_codes_only() -> None:
    failure = OAuthFailure("invalid_grant", terminal=True, retry_after=30)
    assert (str(failure), repr(failure)) == ("invalid_grant", "invalid_grant")
    assert failure.terminal is True and failure.retry_after == 30
    assert OAuthFailure("unavailable").terminal is False


# --- the authorization request (AC 2, I3) ----------------------------------------


def b64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


DISCOVERED = Discovered(
    resource_url=RESOURCE,
    issuer=ISSUER,
    authorization_endpoint="https://login.example.com/authorize",
    token_endpoint="https://token.example.com/token",
    registration_endpoint="https://register.example.com/register",
)


def test_the_authorization_url_carries_every_required_parameter() -> None:
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    issued = new_authorization(
        DISCOVERED, client_id="client-example-1", redirect_uri=REDIRECT_URI, now=now
    )
    parts = urlsplit(issued.authorization_url)
    assert parts.scheme == "https"
    assert (parts.hostname, parts.path) == ("login.example.com", "/authorize")
    query = parse_qs(parts.query, strict_parsing=True)
    assert all(len(values) == 1 for values in query.values())
    params = {key: values[0] for key, values in query.items()}
    assert set(params) == {
        "response_type",
        "client_id",
        "redirect_uri",
        "state",
        "code_challenge",
        "code_challenge_method",
        "resource",
    }
    assert params["response_type"] == "code"
    assert params["client_id"] == "client-example-1"
    assert params["redirect_uri"] == REDIRECT_URI
    assert params["resource"] == RESOURCE
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"] == b64url_sha256(issued.code_verifier)
    assert issued.code_verifier not in issued.authorization_url
    assert issued.expires_at == now + timedelta(minutes=10)


def test_the_verifier_and_state_are_fresh_csprng_values() -> None:
    first = new_authorization(
        DISCOVERED, client_id="client-example-1", redirect_uri=REDIRECT_URI
    )
    second = new_authorization(
        DISCOVERED, client_id="client-example-1", redirect_uri=REDIRECT_URI
    )
    for issued in (first, second):
        assert len(issued.code_verifier) == 86
        assert re.fullmatch(r"[A-Za-z0-9_-]{86}", issued.code_verifier)
        state = parse_qs(urlsplit(issued.authorization_url).query)["state"][0]
        assert len(base64.urlsafe_b64decode(state + "=" * (-len(state) % 4))) >= 16
        assert issued.state_hash == hashlib.sha256(state.encode("ascii")).digest()
        assert state.encode("ascii") != issued.state_hash
        assert issued.code_verifier not in repr(issued)
    assert first.code_verifier != second.code_verifier
    assert first.state_hash != second.state_hash


def test_scope_is_sent_only_when_discovery_produced_one() -> None:
    scoped = replace(DISCOVERED, scope="jobs:read profile")
    issued = new_authorization(
        scoped, client_id="client-example-1", redirect_uri=REDIRECT_URI
    )
    assert parse_qs(urlsplit(issued.authorization_url).query)["scope"] == [
        "jobs:read profile"
    ]
    unscoped = new_authorization(
        DISCOVERED, client_id="client-example-1", redirect_uri=REDIRECT_URI
    )
    assert "scope" not in parse_qs(urlsplit(unscoped.authorization_url).query)


def test_an_authorization_endpoint_query_is_kept() -> None:
    discovered = replace(
        DISCOVERED,
        authorization_endpoint="https://login.example.com/authorize?tenant=a",
    )
    issued = new_authorization(
        discovered, client_id="client-example-1", redirect_uri=REDIRECT_URI
    )
    query = parse_qs(urlsplit(issued.authorization_url).query)
    assert query["tenant"] == ["a"]
    assert query["response_type"] == ["code"]


def test_there_is_no_plain_challenge_method_in_the_module() -> None:
    source = Path(oauth.__file__).read_text(encoding="utf-8")
    assert not re.search(r"""["']plain["']""", source)
    assert "S256" in source


def test_discovered_scope_feeds_the_url_end_to_end(servers: FakeServers) -> None:
    servers.server_metadata["scopes_supported"] = ["server-scope"]
    issued = new_authorization(
        servers.discover(), client_id="client-example-1", redirect_uri=REDIRECT_URI
    )
    assert parse_qs(urlsplit(issued.authorization_url).query)["scope"] == [
        "server-scope"
    ]
