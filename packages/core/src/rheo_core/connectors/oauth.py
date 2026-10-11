"""Core's public OAuth client for a remote MCP resource: discovery and the
authorization request.

:func:`discover` reads the authorization server's configuration from the operator's
resource URL; :func:`new_authorization` builds the PKCE pair, the state and the URL
the owner's browser opens. Every request goes through
:func:`rheo_core.connectors.egress.bounded_request`, so the outbound rules (``https``
only, public addresses only, no redirects, budget and size cap) live in one place.

**Discovery.** One unauthenticated probe first: a single POST of
:data:`~rheo_core.connectors.egress.INITIALIZE_BODY` to the resource URL with no
``Authorization`` header. Only a 401 ``WWW-Authenticate`` header of at most 1 KiB
carrying a ``Bearer`` challenge with a ``scope`` auth-param (RFC 6749 scope-token
characters) is used, for the first-choice scope; any other outcome supplies no scope
and is not a failure, except a redirect, which is refused like every other redirect.
The challenge's ``resource_metadata`` parameter is never fetched: the metadata
locations are computed from the configured URL. Then the RFC 9728 protected-resource
metadata, its first ``authorization_servers`` entry, and that issuer's RFC 8414
metadata, whose ``issuer`` must equal the URL it was fetched for. Each request has a
15 s budget (at most 20 s with the last 5 s read) and a 64 KiB cap; the whole
discovery is cut at 30 s. Every failure is ``endpoint_misconfigured``. The metadata
must advertise S256, the ``authorization_code`` and ``refresh_token`` grants, token
auth method ``none`` and a ``registration_endpoint`` (the client identity comes only
from registration); a missing ``revocation_endpoint`` is accepted. Every discovered
URL must be ``https`` and resolve public.

**Scope.** The challenge's ``scope``, else the protected-resource
``scopes_supported``, else the authorization server's, else none. It is used only in
the authorization URL.

**Failures.** :class:`OAuthFailure` carries fixed codes only; it is raised after the
``except`` block that recorded the code has ended, so ``__cause__`` and
``__context__`` are both ``None`` (the egress module's rule).
"""

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from rheo_core.connectors import egress
from rheo_core.connectors.egress import Clock, EgressResponse, Resolver

OAuthCode = Literal[
    "endpoint_misconfigured",
    "exchange_failed",
    "no_refresh_token",
    "token_refresh_failed",
    "invalid_grant",
    "unauthorized_client",
    "client_rejected",
    "registration_failed",
    "registration_unsupported",
    "unavailable",
    "rate_limited",
    "secret_write_failed",
    "secret_unavailable",
]

REQUEST_BUDGET_SECONDS: Final = 15.0
DISCOVERY_BUDGET_SECONDS: Final = 30.0
METADATA_CAP_BYTES: Final = 64 * 1024
CHALLENGE_HEADER_CAP: Final = 1024
AUTHORIZATION_LIFETIME: Final = timedelta(minutes=10)

PROTECTED_RESOURCE_DOCUMENT: Final = "oauth-protected-resource"
AUTHORIZATION_SERVER_DOCUMENT: Final = "oauth-authorization-server"

_CHALLENGE_METHOD: Final = "S256"
_SCOPE: Final = re.compile(r"[\x21\x23-\x5B\x5D-\x7E]+(?: [\x21\x23-\x5B\x5D-\x7E]+)*")
_SCOPE_TOKEN: Final = re.compile(r"[\x21\x23-\x5B\x5D-\x7E]+")
_TOKEN: Final = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_QUOTED: Final = re.compile(r'"((?:[^"\\]|\\.)*)"')
_QUOTED_PAIR: Final = re.compile(r"\\(.)")


class OAuthFailure(Exception):
    """A refused or failed OAuth step, carrying a fixed code only."""

    def __init__(
        self, code: OAuthCode, terminal: bool = False, retry_after: int | None = None
    ) -> None:
        super().__init__(code)
        self.code: OAuthCode = code
        self.terminal = terminal
        self.retry_after = retry_after

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class Discovered:
    resource_url: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    revocation_endpoint: str | None = None
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class NewAuthorization:
    """What connect stores and returns. The raw state appears only in the URL."""

    authorization_url: str
    state_hash: bytes
    code_verifier: str = field(repr=False)
    expires_at: datetime


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _misconfigured() -> OAuthFailure:
    return OAuthFailure("endpoint_misconfigured")


def _public_https(url: object, resolver: Resolver) -> str:
    """``url`` itself when it is an ``https`` URL on a public host, else refused."""
    refused = True
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
            parts.port  # noqa: B018 - raises ValueError for a malformed port
            host = parts.hostname
            if parts.scheme == "https" and host:
                egress.check_public(host, resolver)
                refused = False
        except (ValueError, egress.EgressFailure):
            refused = True
    if refused or not isinstance(url, str):
        raise _misconfigured()
    return url


def _well_known(url: str, name: str) -> str:
    """RFC 9728 s3.1 / RFC 8414 s3.1: ``/.well-known/<name>`` goes between the
    origin and the path, the path's trailing slash dropped."""
    parts = urlsplit(url)
    path = f"/.well-known/{name}{parts.path.rstrip('/')}"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    deadline: float,
    resolver: Resolver,
    clock: Clock,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> EgressResponse | egress.EgressCode:
    """One discovery request clipped to the whole-discovery deadline; returns the
    response or the egress failure code (never raises one)."""
    budget = min(REQUEST_BUDGET_SECONDS, deadline - clock())
    if budget <= 0:
        return "timeout"
    try:
        return egress.bounded_request(
            client,
            method,
            url,
            budget=budget,
            cap=METADATA_CAP_BYTES,
            resolver=resolver,
            clock=clock,
            headers=headers,
            content=content,
        )
    except egress.EgressFailure as failure:
        return failure.code


def _skip_space(value: str, pos: int) -> int:
    while pos < len(value) and value[pos] in " \t":
        pos += 1
    return pos


def _parse_challenges(value: str) -> list[tuple[str, dict[str, str]]] | None:
    """RFC 9110 s11.6.1 challenges as ``(scheme lowercased, params)``; ``None`` when
    malformed (a token68 credential, a duplicate parameter, stray characters)."""
    challenges: list[tuple[str, dict[str, str]]] = []
    pos = 0
    length = len(value)
    while pos < length:
        pos = _skip_space(value, pos)
        if pos < length and value[pos] == ",":
            pos += 1
            continue
        if pos >= length:
            break
        token = _TOKEN.match(value, pos)
        if token is None:
            return None
        after = _skip_space(value, token.end())
        if after < length and value[after] == "=":
            if not challenges:
                return None
            pos = _skip_space(value, after + 1)
            quoted = _QUOTED.match(value, pos)
            if quoted is not None:
                param = _QUOTED_PAIR.sub(r"\1", quoted.group(1))
                pos = quoted.end()
            else:
                bare = _TOKEN.match(value, pos)
                if bare is None:
                    return None
                param = bare.group(0)
                pos = bare.end()
            name = token.group(0).lower()
            params = challenges[-1][1]
            if name in params:
                return None
            params[name] = param
            pos = _skip_space(value, pos)
            if pos < length and value[pos] != ",":
                return None
        else:
            challenges.append((token.group(0).lower(), {}))
            pos = after
    return challenges


def _challenge_scope(response: EgressResponse | egress.EgressCode) -> str | None:
    """The probe's ``Bearer`` challenge scope, or ``None`` for any other outcome."""
    if not isinstance(response, EgressResponse) or response.status != 401:
        return None
    values = response.headers.get_list("www-authenticate")
    combined = ", ".join(values)
    if not values or len(combined.encode("utf-8")) > CHALLENGE_HEADER_CAP:
        return None
    challenges = _parse_challenges(combined)
    if challenges is None:
        return None
    for scheme, params in challenges:
        if scheme == "bearer":
            scope = params.get("scope")
            if scope is not None and _SCOPE.fullmatch(scope):
                return scope
            return None
    return None


def _metadata(
    client: httpx.Client,
    url: str,
    *,
    deadline: float,
    resolver: Resolver,
    clock: Clock,
) -> dict[str, object]:
    """GET one metadata document: a 200 JSON object, else refused."""
    response = _request(
        client, "GET", url, deadline=deadline, resolver=resolver, clock=clock
    )
    document: object = None
    if isinstance(response, EgressResponse) and response.status == 200:
        try:
            document = json.loads(response.body)
        except (ValueError, RecursionError):
            document = None
    if not isinstance(document, dict):
        raise _misconfigured()
    return document


def _strings(document: dict[str, object], key: str) -> list[str] | None:
    """A list-of-strings member; ``None`` when absent, refused when malformed."""
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _misconfigured()
    return list(value)


def _scopes(document: dict[str, object]) -> str | None:
    """``scopes_supported`` joined with single spaces; ``None`` when absent or empty."""
    scopes = _strings(document, "scopes_supported")
    if not scopes:
        return None
    if not all(_SCOPE_TOKEN.fullmatch(s) for s in scopes):
        raise _misconfigured()
    return " ".join(scopes)


def discover(
    resource_url: str,
    *,
    resolver: Resolver = egress.default_resolver,
    clock: Clock = time.monotonic,
    transport: httpx.BaseTransport | None = None,
) -> Discovered:
    """Discover the authorization server for ``resource_url`` (module docstring)."""
    resource_url = _public_https(resource_url, resolver)
    deadline = clock() + DISCOVERY_BUDGET_SECONDS
    with egress.open_client(transport) as client:
        probe = _request(
            client,
            "POST",
            resource_url,
            deadline=deadline,
            resolver=resolver,
            clock=clock,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            content=egress.INITIALIZE_BODY,
        )
        if probe == "endpoint_misconfigured":
            raise _misconfigured()
        challenge_scope = _challenge_scope(probe)

        resource_document = _metadata(
            client,
            _well_known(resource_url, PROTECTED_RESOURCE_DOCUMENT),
            deadline=deadline,
            resolver=resolver,
            clock=clock,
        )
        servers = _strings(resource_document, "authorization_servers")
        if not servers:
            raise _misconfigured()
        issuer = _public_https(servers[0], resolver)
        resource_scope = _scopes(resource_document)

        server_document = _metadata(
            client,
            _well_known(issuer, AUTHORIZATION_SERVER_DOCUMENT),
            deadline=deadline,
            resolver=resolver,
            clock=clock,
        )
    if server_document.get("issuer") != issuer:
        raise _misconfigured()
    methods = _strings(server_document, "code_challenge_methods_supported") or []
    grants = _strings(server_document, "grant_types_supported") or []
    auth_methods = (
        _strings(server_document, "token_endpoint_auth_methods_supported") or []
    )
    if (
        _CHALLENGE_METHOD not in methods
        or "authorization_code" not in grants
        or "refresh_token" not in grants
        or "none" not in auth_methods
    ):
        raise _misconfigured()
    server_scope = _scopes(server_document)
    revocation = server_document.get("revocation_endpoint")
    return Discovered(
        resource_url=resource_url,
        issuer=issuer,
        authorization_endpoint=_public_https(
            server_document.get("authorization_endpoint"), resolver
        ),
        token_endpoint=_public_https(server_document.get("token_endpoint"), resolver),
        registration_endpoint=_public_https(
            server_document.get("registration_endpoint"), resolver
        ),
        revocation_endpoint=(
            None if revocation is None else _public_https(revocation, resolver)
        ),
        scope=challenge_scope or resource_scope or server_scope,
    )


def new_authorization(
    discovered: Discovered,
    *,
    client_id: str,
    redirect_uri: str,
    now: datetime | None = None,
) -> NewAuthorization:
    """A fresh PKCE pair and state, and the authorization URL carrying them.

    The verifier is 64 CSPRNG bytes (86 base64url characters) and never enters the
    URL; the state is 32 CSPRNG bytes, of which only the SHA-256 is returned.
    """
    verifier = _b64url(secrets.token_bytes(64))
    state = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    params = [
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("state", state),
        ("code_challenge", challenge),
        ("code_challenge_method", _CHALLENGE_METHOD),
        ("resource", discovered.resource_url),
    ]
    if discovered.scope:
        params.append(("scope", discovered.scope))
    parts = urlsplit(discovered.authorization_endpoint)
    query = urlencode(params, quote_via=quote)
    if parts.query:
        query = f"{parts.query}&{query}"
    issued = now if now is not None else datetime.now(UTC)
    return NewAuthorization(
        authorization_url=urlunsplit(
            (parts.scheme, parts.netloc, parts.path, query, "")
        ),
        state_hash=hashlib.sha256(state.encode("ascii")).digest(),
        code_verifier=verifier,
        expires_at=issued + AUTHORIZATION_LIFETIME,
    )
