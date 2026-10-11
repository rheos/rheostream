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
metadata, whose ``resource`` must equal the configured URL (RFC 9728 s3.3), its
first ``authorization_servers`` entry, and that issuer's RFC 8414 metadata, whose
``issuer`` must equal the URL it was fetched for. Each request has a 15 s budget (at
most 20 s with the last 5 s read) and a 64 KiB cap; the whole discovery is cut at
30 s, a deadline started before the first host lookup and checked again after the
last endpoint's, so slow DNS counts too. Every failure is ``endpoint_misconfigured``.
The metadata must advertise S256, the ``authorization_code`` and ``refresh_token``
grants, token auth method ``none`` and a ``registration_endpoint`` (the client
identity comes only from registration); a missing ``revocation_endpoint`` is
accepted. Every discovered URL must be ``https`` and resolve public.

**Scope.** The challenge's ``scope``, else the protected-resource
``scopes_supported``, else the authorization server's, else none. It is used only in
the authorization URL.

**Operator switch.** :func:`enabled` is true only when
``connectors.oauth_client.enabled`` is on and ``routing.identity.path`` is the served
identity prefix (:data:`rheo_core.oauth.surface.SERVED_IDENTITY_PATH`), the only
prefix the callback route answers under. Otherwise :func:`redirect_uri` and
:func:`register` refuse ``endpoint_misconfigured`` before any request.
:func:`redirect_uri` is built by ``url_for`` from the routing settings alone, never
from request data.

**Registration (RFC 7591).** :func:`register` sends exactly one ``POST`` of
``application/json`` with no ``Authorization`` header to the pinned registration
endpoint, whose body is exactly ``redirect_uris``, ``grant_types``,
``response_types``, ``token_endpoint_auth_method`` (``none``) and ``client_name``. It
is never retried. Only a 200 or 201 JSON object with a ``client_id`` of 1 to 255
printable ASCII characters is accepted; an echoed ``redirect_uris`` must equal the
request and an echoed ``grant_types`` must include both grants. A 3xx (or any other
outbound-rule refusal) is ``endpoint_misconfigured``; an unsupported or secretless
secret-based auth method is ``registration_unsupported``; anything else, including a
timeout or a body over 16 KiB, is ``registration_failed``. A refusal stores nothing.

**Secrets.** A ``client_secret`` and a ``registration_access_token`` are written as
files under ``secret://file/ws/<workspace>/connection/<connection>/oauth/<uuid7>``
through this module's own writable scope, and only their references leave it. A
reference is a string; its bytes are read again only by
:func:`apply_client_authentication`, for the two secret-based methods. The
registration access token is never sent anywhere. :func:`discard` removes one file
of exactly one connection's ``oauth/`` namespace, best-effort, logging only the fixed
codes ``secret_discard_refused`` and ``secret_discard_failed``.

**Client authentication.** :func:`apply_client_authentication` adds a stored client's
credentials to a token or revocation request: ``none`` puts ``client_id`` in the form
body; ``client_secret_basic`` sets HTTP Basic over the form-urlencoded client id and
secret (RFC 6749 s2.3.1) with no secret in the body; ``client_secret_post`` puts both
in the body. A secret whose stored expiry has passed, or that cannot be read, is
never sent: either is the terminal ``client_rejected``.

**Failures.** :class:`OAuthFailure` carries fixed codes only; it is raised after the
``except`` block that recorded the code has ended, so ``__cause__`` and
``__context__`` are both ``None`` (the egress module's rule).
"""

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from urllib.parse import quote, quote_plus, urlencode, urlsplit, urlunsplit
from uuid import UUID

import httpx

from rheo_core.connectors import egress
from rheo_core.connectors.egress import Clock, EgressResponse, Resolver
from rheo_core.oauth.surface import SERVED_IDENTITY_PATH
from rheo_core.refs import uuid7
from rheo_core.routing import IDENTITY, RoutingConfig, url_for
from rheo_core.secrets import (
    SecretRef,
    SecretRefusal,
    SecretScope,
    SecretStore,
    SecretValue,
)
from rheo_core.settings import resolve
from rheo_core.settings.schema import SettingsError
from rheo_core.storage.data_root import resolve_data_root

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

ClientAuthMethod = Literal["none", "client_secret_basic", "client_secret_post"]

ENABLED_KEY: Final = "connectors.oauth_client.enabled"
REGISTRATION_CAP_BYTES: Final = 16 * 1024
CLIENT_ID_MAX_LENGTH: Final = 255
GRANT_TYPES: Final = ("authorization_code", "refresh_token")
SECRET_DISCARD_REFUSED: Final = "secret_discard_refused"
SECRET_DISCARD_FAILED: Final = "secret_discard_failed"

_SCOPE_COMPONENT: Final = "connector-oauth"
_AUTH_METHODS: Final[frozenset[str]] = frozenset(
    {"none", "client_secret_basic", "client_secret_post"}
)
_SECRET_METHODS: Final[frozenset[str]] = frozenset(
    {"client_secret_basic", "client_secret_post"}
)
_MODULE_ID: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
_PRINTABLE_ASCII: Final = re.compile(r"[\x20-\x7e]+")
_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_OAUTH_REFERENCE: Final = re.compile(
    rf"secret://file/ws/({_UUID})/connection/({_UUID})/oauth/{_UUID}"
)
_EGRESS_TO_REGISTRATION: Final[dict[egress.EgressCode, OAuthCode]] = {
    "endpoint_misconfigured": "endpoint_misconfigured",
    "timeout": "registration_failed",
    "too_large": "registration_failed",
    "unavailable": "registration_failed",
}

_log = logging.getLogger("rheo_core.connectors.oauth")


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


@dataclass(frozen=True, slots=True)
class RegisteredClient:
    """What a successful registration returns. The two secrets exist only as files;
    their references are strings and are left out of ``repr``."""

    client_id: str
    auth_method: ClientAuthMethod
    redirect_uri: str
    secret_expires_at: datetime | None
    client_secret_ref: str | None = field(repr=False)
    registration_token_ref: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class ClientAuth:
    """A stored client as the token and revocation requests see it (built by its
    owner from plain columns). The registration access token is not part of it."""

    client_id: str
    auth_method: ClientAuthMethod
    client_secret_ref: str | None = field(repr=False)
    secret_expires_at: datetime | None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _misconfigured() -> OAuthFailure:
    return OAuthFailure("endpoint_misconfigured")


def _public_https(
    url: object, resolver: Resolver, *, deadline: float, clock: Clock
) -> str:
    """``url`` itself when it is an ``https`` URL on a public host, resolved before
    ``deadline``, else refused."""
    refused = True
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
            parts.port  # noqa: B018 - raises ValueError for a malformed port
            host = parts.hostname
            if parts.scheme == "https" and host:
                egress.check_public(host, resolver, deadline=deadline, clock=clock)
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
    deadline = clock() + DISCOVERY_BUDGET_SECONDS

    def checked(url: object) -> str:
        return _public_https(url, resolver, deadline=deadline, clock=clock)

    resource_url = checked(resource_url)
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
        if resource_document.get("resource") != resource_url:
            raise _misconfigured()
        servers = _strings(resource_document, "authorization_servers")
        if not servers:
            raise _misconfigured()
        issuer = checked(servers[0])
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
    discovered = Discovered(
        resource_url=resource_url,
        issuer=issuer,
        authorization_endpoint=checked(server_document.get("authorization_endpoint")),
        token_endpoint=checked(server_document.get("token_endpoint")),
        registration_endpoint=checked(server_document.get("registration_endpoint")),
        revocation_endpoint=None if revocation is None else checked(revocation),
        scope=challenge_scope or resource_scope or server_scope,
    )
    if clock() >= deadline:
        raise _misconfigured()
    return discovered


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


# --- the secret namespace -----------------------------------------------------------


def _namespace(workspace_id: UUID | str, connection_id: UUID | str) -> str:
    return f"secret://file/ws/{workspace_id}/connection/{connection_id}/oauth/"


def _scope(namespace: str, *, writable: bool) -> SecretScope:
    """This module's scope over ONE connection's ``oauth/`` namespace."""
    return SecretStore.scope_for(_SCOPE_COMPONENT, namespace, writable=writable)


def _store() -> SecretStore:
    return SecretStore(resolve_data_root().path)


def _reference_namespace(ref: object) -> str | None:
    """The ``oauth/`` namespace a reference belongs to, or ``None`` unless the
    reference is exactly ``secret://file/ws/<uuid>/connection/<uuid>/oauth/<uuid>``."""
    match = _OAUTH_REFERENCE.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        return None
    return _namespace(match.group(1), match.group(2))


def new_reference(workspace_id: UUID, connection_id: UUID) -> str:
    """A fresh reference in the connection's ``oauth/`` namespace (a ``uuid7`` leaf)."""
    return f"{_namespace(workspace_id, connection_id)}{uuid7()}"


def discard(ref: str) -> None:
    """Remove the one file ``ref`` names, best-effort; never raises.

    Only a reference of exactly the ``oauth/`` shape is acted on, through a scope
    covering that reference's own connection namespace and nothing wider; any other
    reference is refused without touching the store.
    """
    namespace = _reference_namespace(ref)
    if namespace is None:
        _log.warning(SECRET_DISCARD_REFUSED)
        return
    failed = False
    try:
        _store().discard(SecretRef.parse(ref), _scope(namespace, writable=True))
    except Exception:  # best-effort: the caller's outcome stands whatever happens
        failed = True
    if failed:
        _log.warning(SECRET_DISCARD_FAILED)


# --- the operator switch and the redirect URI -------------------------------------


def _configured_routing() -> RoutingConfig | None:
    """The routing configuration when the connector client is configured."""
    settings = resolve()
    if not settings.get_bool(ENABLED_KEY):
        return None
    try:
        routing = RoutingConfig.from_settings(settings)
    except (SettingsError, ValueError):
        return None
    if routing.surfaces.identity.path != SERVED_IDENTITY_PATH:
        return None
    return routing


def enabled() -> bool:
    """Whether the connector OAuth client is configured (module docstring)."""
    return _configured_routing() is not None


def redirect_uri(module_id: str) -> str:
    """The callback URI registered for ``module_id``, from routing settings only."""
    routing = _configured_routing()
    if (
        routing is None
        or not isinstance(module_id, str)
        or not _MODULE_ID.fullmatch(module_id)
    ):
        raise _misconfigured()
    return url_for(routing, IDENTITY, f"/connector/{module_id}/callback")


# --- registration -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Accepted:
    client_id: str
    auth_method: ClientAuthMethod
    secret_expires_at: datetime | None
    client_secret: bytes | None = field(repr=False)
    registration_token: bytes | None = field(repr=False)


def _registration_body(redirect_uri: str, client_name: str) -> bytes:
    """The exact registration request body: these five fields and nothing else."""
    return json.dumps(
        {
            "redirect_uris": [redirect_uri],
            "grant_types": list(GRANT_TYPES),
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_name": client_name,
        },
        separators=(",", ":"),
    ).encode("ascii")


def _optional_secret(document: dict[str, object], key: str) -> bytes | None | OAuthCode:
    """A string secret member as bytes; ``None`` when absent, null or empty."""
    value = document.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return "registration_failed"
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from a JSON escape
        return "registration_failed"


def _expiry(value: object) -> datetime | None | OAuthCode:
    """``client_secret_expires_at``: ``None`` when absent or zero (no expiry)."""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "registration_failed"
    if value == 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return "registration_failed"


def _judge_registration(
    response: EgressResponse, redirect_uri: str
) -> _Accepted | OAuthCode:
    """The accepted registration, or the refusal code; never raises."""
    if response.status not in (200, 201):
        return "registration_failed"
    try:
        document = json.loads(response.body)
    except (ValueError, RecursionError):
        return "registration_failed"
    if not isinstance(document, dict):
        return "registration_failed"
    client_id = document.get("client_id")
    if (
        not isinstance(client_id, str)
        or len(client_id) > CLIENT_ID_MAX_LENGTH
        or not _PRINTABLE_ASCII.fullmatch(client_id)
    ):
        return "registration_failed"
    if "redirect_uris" in document and document["redirect_uris"] != [redirect_uri]:
        return "registration_failed"
    if "grant_types" in document:
        echoed = document["grant_types"]
        if not isinstance(echoed, list) or not all(g in echoed for g in GRANT_TYPES):
            return "registration_failed"
    secret = _optional_secret(document, "client_secret")
    token = _optional_secret(document, "registration_access_token")
    expires_at = _expiry(document.get("client_secret_expires_at"))
    if isinstance(secret, str) or isinstance(token, str):
        return "registration_failed"
    if isinstance(expires_at, str):
        return expires_at
    method = document.get("token_endpoint_auth_method")
    if method is None:
        method = "none" if secret is None else "client_secret_basic"
    if not isinstance(method, str):
        return "registration_failed"
    if method not in _AUTH_METHODS or (method in _SECRET_METHODS and secret is None):
        return "registration_unsupported"
    auth_method: ClientAuthMethod
    if method == "client_secret_basic":
        auth_method = "client_secret_basic"
    elif method == "client_secret_post":
        auth_method = "client_secret_post"
    else:
        auth_method = "none"
    return _Accepted(
        client_id=client_id,
        auth_method=auth_method,
        secret_expires_at=expires_at,
        client_secret=secret,
        registration_token=token,
    )


def _write_secrets(
    values: tuple[bytes | None, ...], workspace_id: UUID, connection_id: UUID
) -> list[str | None] | None:
    """Write each present value to a fresh file; the references, or ``None`` when a
    write failed (every file already written, and the failed one, discarded)."""
    written: list[str | None] = []
    for raw in values:
        if raw is None:
            written.append(None)
            continue
        ref = new_reference(workspace_id, connection_id)
        failed = False
        try:
            namespace = _namespace(workspace_id, connection_id)
            _store().create(
                SecretRef.parse(ref),
                SecretValue(raw),
                _scope(namespace, writable=True),
            )
        except (SecretRefusal, OSError, ValueError, TypeError):
            failed = True
        if failed:
            # The failed leaf is fresh and owned by nobody else, so discarding it
            # also covers a write that published before it reported failure.
            for earlier in (*written, ref):
                if earlier is not None:
                    discard(earlier)
            return None
        written.append(ref)
    return written


def register(
    registration_endpoint: str,
    redirect_uri: str,
    client_name: str,
    *,
    workspace_id: UUID,
    connection_id: UUID,
    resolver: Resolver = egress.default_resolver,
    clock: Clock = time.monotonic,
    transport: httpx.BaseTransport | None = None,
) -> RegisteredClient:
    """Register a client at the pinned endpoint, once (module docstring)."""
    if (
        not enabled()
        or not isinstance(registration_endpoint, str)
        or not isinstance(redirect_uri, str)
        or not isinstance(client_name, str)
    ):
        raise _misconfigured()
    body = _registration_body(redirect_uri, client_name)
    code: OAuthCode | None = None
    response: EgressResponse | None = None
    with egress.open_client(transport) as client:
        try:
            response = egress.bounded_request(
                client,
                "POST",
                registration_endpoint,
                budget=REQUEST_BUDGET_SECONDS,
                cap=REGISTRATION_CAP_BYTES,
                resolver=resolver,
                clock=clock,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                content=body,
            )
        except egress.EgressFailure as failure:
            code = _EGRESS_TO_REGISTRATION[failure.code]
    if code is not None or response is None:
        raise OAuthFailure(code or "registration_failed")
    accepted = _judge_registration(response, redirect_uri)
    if isinstance(accepted, str):
        raise OAuthFailure(accepted)
    refs = _write_secrets(
        (accepted.client_secret, accepted.registration_token),
        workspace_id,
        connection_id,
    )
    if refs is None:
        raise OAuthFailure("registration_failed")
    return RegisteredClient(
        client_id=accepted.client_id,
        auth_method=accepted.auth_method,
        redirect_uri=redirect_uri,
        secret_expires_at=accepted.secret_expires_at,
        client_secret_ref=refs[0],
        registration_token_ref=refs[1],
    )


# --- client authentication ----------------------------------------------------------


def _read_client_secret(ref: str | None) -> str | None:
    """The client secret behind ``ref``, or ``None`` when it cannot be read."""
    namespace = _reference_namespace(ref)
    if namespace is None:
        return None
    try:
        value = _store().resolve(
            SecretRef.parse(ref), _scope(namespace, writable=False)
        )
        return value.expose().decode("utf-8")
    except Exception:  # any failure means the stored client cannot authenticate
        return None


def apply_client_authentication(
    client: ClientAuth,
    form: MutableMapping[str, str],
    headers: MutableMapping[str, str],
    *,
    now: datetime,
) -> None:
    """Add ``client``'s credentials to a token or revocation request (module
    docstring). Raises the terminal ``client_rejected`` with nothing added when a
    secret-based client's secret has expired or cannot be read."""
    method = client.auth_method
    if method == "none":
        form["client_id"] = client.client_id
        return
    if method not in _SECRET_METHODS or (
        client.secret_expires_at is not None and client.secret_expires_at <= now
    ):
        raise OAuthFailure("client_rejected", terminal=True)
    secret = _read_client_secret(client.client_secret_ref)
    if secret is None:
        raise OAuthFailure("client_rejected", terminal=True)
    if method == "client_secret_basic":
        pair = f"{quote_plus(client.client_id)}:{quote_plus(secret)}"
        encoded = base64.b64encode(pair.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    else:
        form["client_id"] = client.client_id
        form["client_secret"] = secret
