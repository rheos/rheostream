"""Core's public OAuth client for a remote MCP resource: discovery, the
authorization request, client registration and the token operations.

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

**Operator switch and redirect mode.** ``connectors.oauth_client.redirect`` picks
where the provider sends the owner's browser back to. ``identity`` (the default) is
the identity-host callback route: :func:`enabled` is then true only when
``connectors.oauth_client.enabled`` is on and ``routing.identity.path`` is the served
identity prefix (:data:`rheo_core.oauth.surface.SERVED_IDENTITY_PATH`), the only
prefix that route answers under, and :func:`redirect_uri` is built by ``url_for``
from the routing settings. ``loopback`` is for providers that register only loopback
redirect URIs: the URI is exactly ``http://127.0.0.1:<port>/callback`` with the
operator's ``connectors.oauth_client.loopback_port``, nothing listens there, and the
owner pastes the address the browser lands on back into the module
(:func:`loopback_callback_parameters` reads it). That mode reaches no route of this
deployment, so the identity path does not gate it. Either way the URI comes from
settings alone, never from request data, and while :func:`enabled` is false
:func:`redirect_uri` and :func:`register` refuse ``endpoint_misconfigured`` before
any request. :func:`callback_served` says whether the identity-host route answers.

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
never sent: either is the terminal ``client_rejected``, and so is a naive expiry or
``now`` (the comparison never raises).

**Tokens.** :func:`exchange_code` and :func:`refresh` each send one form ``POST``
to the pinned token endpoint, authenticated per the stored client, with a 15 s
budget and a 16 KiB cap, and carry the pinned ``resource`` (RFC 8707); a refresh
sends no ``scope``. Only a 200 JSON object with a ``bearer`` ``token_type`` (any
case) and an ``access_token`` of visible ASCII is accepted. The exchange also needs
a ``refresh_token`` (else ``no_refresh_token``, nothing written). A refresh token is
written to a fresh leaf before its reference is returned; a refresh returns a new
reference only when the token differs from the current one (constant-time compare),
and a failed write is ``secret_write_failed`` with the current reference untouched.
A refresh that writes nothing cannot fail to write, so its ``secret_write_failed``
always means the provider answered with a different refresh token; a provider that
rotates on use has already spent the current one. Whether that is terminal is the
caller's call. Retiring a superseded file is the caller's job, after its switch
commits. A refresh
whose current reference cannot be read is the terminal ``secret_unavailable`` with
nothing sent. Mapping: a 400 or 401 ``invalid_client`` is the terminal
``client_rejected``; ``invalid_grant`` and ``unauthorized_client`` are terminal
under their own codes; 429 is ``rate_limited`` with a seconds-form ``Retry-After``
as ``retry_after``; a timeout or transport error is ``unavailable``; a refused
endpoint (redirect, non-public, not ``https``) is ``endpoint_misconfigured``; any
other answer is ``exchange_failed`` or ``token_refresh_failed``. Only the terminal
ones set ``terminal``. Nothing here registers a client or retries.

**Revocation.** :func:`revoke` sends the refresh token (``token_type_hint``
``refresh_token``, no ``resource``) to the pinned revocation endpoint, authenticated
per the stored client, and reports a :class:`Revocation`: 200 is ``revoked``, no
pinned endpoint is ``revocation_unavailable`` with nothing sent, anything else is
``revocation_failed``, with ``client_rejected`` set for an ``invalid_client`` answer
or a client secret that is expired or unreadable (then nothing is sent). It never
raises; an unexpected exception is logged by its type name only.

**Access token.** :class:`AccessToken` holds the access token for one run and gives
its bytes only through :meth:`AccessToken.authorization_header`.

**Failures.** :class:`OAuthFailure` carries fixed codes only; it is raised after the
``except`` block that recorded the code has ended, so ``__cause__`` and
``__context__`` are both ``None`` (the egress module's rule).
"""

import base64
import enum
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, final
from urllib.parse import (
    parse_qsl,
    quote,
    quote_plus,
    urlencode,
    urlsplit,
    urlunsplit,
)
from uuid import UUID

import httpx

from rheo_core.connectors import credentials, egress
from rheo_core.connectors.egress import Clock, EgressResponse, Resolver
from rheo_core.modules.manifest import check_module_id
from rheo_core.oauth.surface import SERVED_IDENTITY_PATH
from rheo_core.refs import uuid7
from rheo_core.routing import IDENTITY, RoutingConfig, url_for
from rheo_core.secrets import (
    OpaqueValue,
    SecretRef,
    SecretRefusal,
    SecretScope,
    SecretStore,
    SecretValue,
)
from rheo_core.settings import resolve
from rheo_core.settings.schema import SettingsError

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
REDIRECT_KEY: Final = "connectors.oauth_client.redirect"
LOOPBACK_PORT_KEY: Final = "connectors.oauth_client.loopback_port"
LOOPBACK_HOST: Final = "127.0.0.1"
LOOPBACK_PATH: Final = "/callback"
RedirectMode = Literal["identity", "loopback"]
CALLBACK_PARAMETERS: Final = ("state", "code", "error", "iss")
"""The only callback parameters ever read, from the route or a pasted address."""
MAX_CALLBACK_PARAMETER_LENGTH: Final = 2048
"""The longest value accepted for any one of :data:`CALLBACK_PARAMETERS`."""
MAX_PASTED_ADDRESS_LENGTH: Final = 4096
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


def _connection_namespace(workspace_id: UUID, connection_id: UUID) -> str:
    """The ``oauth/`` namespace for two ``UUID`` objects; anything else is refused
    ``endpoint_misconfigured``, so no file is ever written under a name
    :func:`discard` would not accept."""
    if not isinstance(workspace_id, UUID) or not isinstance(connection_id, UUID):
        raise _misconfigured()
    return _namespace(workspace_id, connection_id)


def _reference_namespace(ref: object) -> str | None:
    """The ``oauth/`` namespace a reference belongs to, or ``None`` unless the
    reference is exactly ``secret://file/ws/<uuid>/connection/<uuid>/oauth/<uuid>``."""
    match = _OAUTH_REFERENCE.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        return None
    return _namespace(match.group(1), match.group(2))


def new_reference(workspace_id: UUID, connection_id: UUID) -> str:
    """A fresh reference in the connection's ``oauth/`` namespace (a ``uuid7`` leaf).
    Both ids must be ``UUID`` objects, else ``endpoint_misconfigured``."""
    return f"{_connection_namespace(workspace_id, connection_id)}{uuid7()}"


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
        credentials.store().discard(
            SecretRef.parse(ref), _scope(namespace, writable=True)
        )
    except Exception:  # best-effort: the caller's outcome stands whatever happens
        failed = True
    if failed:
        _log.warning(SECRET_DISCARD_FAILED)


# --- the operator switch and the redirect URI -------------------------------------


def redirect_mode() -> RedirectMode | None:
    """The configured redirect mode, or ``None`` while the client is not configured
    (switched off, or identity mode under an identity path the route does not serve)."""
    settings = resolve()
    if not settings.get_bool(ENABLED_KEY):
        return None
    if settings.get_str(REDIRECT_KEY) == "loopback":
        return "loopback"
    try:
        routing = RoutingConfig.from_settings(settings)
    except (SettingsError, ValueError):
        return None
    if routing.surfaces.identity.path != SERVED_IDENTITY_PATH:
        return None
    return "identity"


def enabled() -> bool:
    """Whether the connector OAuth client is configured (module docstring)."""
    return redirect_mode() is not None


def callback_served() -> bool:
    """Whether the identity-host callback route answers: identity mode only."""
    return redirect_mode() == "identity"


def loopback_redirect_uri() -> str:
    """``http://127.0.0.1:<port>/callback`` for the configured port. The one place
    the loopback URI is built; the port's range is held by its settings key."""
    port = resolve().get_int(LOOPBACK_PORT_KEY)
    return f"http://{LOOPBACK_HOST}:{port}{LOOPBACK_PATH}"


def redirect_uri(module_id: str) -> str:
    """The redirect URI registered for ``module_id``, from settings only."""
    mode = redirect_mode()
    valid = True
    try:
        check_module_id(module_id)
    except ValueError:  # its message names the id; only the fixed code leaves
        valid = False
    if mode is None or not valid:
        raise _misconfigured()
    if mode == "loopback":
        return loopback_redirect_uri()
    return url_for(
        RoutingConfig.from_settings(resolve()),
        IDENTITY,
        f"/connector/{module_id}/callback",
    )


def loopback_callback_parameters(address: object) -> dict[str, str] | None:
    """The callback parameters of an address the owner pasted, or ``None`` unless it
    is exactly the loopback redirect URI plus a query.

    Surrounding whitespace is ignored. The scheme must be ``http``, the authority
    exactly ``127.0.0.1:<configured port>`` (so no user information, no other host
    spelling and no default port), the path exactly ``/callback``; a fragment
    (even an empty ``#``) refuses. Only :data:`CALLBACK_PARAMETERS` are read, each
    at most once and at most :data:`MAX_CALLBACK_PARAMETER_LENGTH` characters, as
    the route reads them; every other parameter is ignored. ``state`` and one of
    ``code`` or ``error`` must be present. Never raises, and nothing about the
    address leaves this function but the parameters.
    """
    if not isinstance(address, str) or len(address) > MAX_PASTED_ADDRESS_LENGTH:
        return None
    text = address.strip()
    found: dict[str, str] = {}
    valid = False
    try:
        parts = urlsplit(text)
        expected = f"{LOOPBACK_HOST}:{resolve().get_int(LOOPBACK_PORT_KEY)}"
        if (
            "#" not in text
            and parts.scheme == "http"
            and parts.netloc == expected
            and parts.path == LOOPBACK_PATH
        ):
            valid = True
            for name, value in parse_qsl(
                parts.query, keep_blank_values=True, max_num_fields=256
            ):
                if name not in CALLBACK_PARAMETERS:
                    continue
                if name in found or len(value) > MAX_CALLBACK_PARAMETER_LENGTH:
                    valid = False
                    break
                found[name] = value
    except (ValueError, SettingsError):  # the parser's text quotes the address
        valid = False
    if not valid or "state" not in found:
        return None
    if "code" not in found and "error" not in found:
        return None
    return found


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


class _Malformed(enum.Enum):
    """The sentinel :func:`_optional_secret` returns for a member that is present
    but not a usable string; each caller maps it to its own code."""

    MEMBER = "malformed"


def _optional_secret(
    document: dict[str, object], key: str
) -> bytes | None | _Malformed:
    """A string secret member as bytes; ``None`` when absent, null or empty."""
    value = document.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return _Malformed.MEMBER
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from a JSON escape
        return _Malformed.MEMBER


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
    if isinstance(secret, _Malformed) or isinstance(token, _Malformed):
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
            namespace = _connection_namespace(workspace_id, connection_id)
            credentials.store().create(
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
        or not isinstance(workspace_id, UUID)
        or not isinstance(connection_id, UUID)
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
        value = credentials.store().resolve(
            SecretRef.parse(ref), _scope(namespace, writable=False)
        )
        return value.expose().decode("utf-8")
    except Exception:  # any failure means the stored client cannot authenticate
        return None


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def _secret_current(expires_at: datetime | None, now: datetime) -> bool:
    """Whether a client secret may still be sent: no expiry, or an expiry after
    ``now``. A naive or non-datetime time on either side counts as expired, so a
    comparison can never raise."""
    if not _aware(now):
        return False
    if expires_at is None:
        return True
    return _aware(expires_at) and expires_at > now


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
    if method not in _SECRET_METHODS or not _secret_current(
        client.secret_expires_at, now
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


# --- the access token ---------------------------------------------------------------

ACCESS_TOKEN_REDACTED: Final = "AccessToken(<redacted>)"


@final
class AccessToken(OpaqueValue):
    """An access token held in memory for one run.

    ``repr``, ``str`` and every ``format`` spec are ``AccessToken(<redacted>)``; it
    cannot be hashed, pickled, copied, subclassed or serialised (the guards of
    :class:`~rheo_core.secrets.OpaqueValue`, shared with ``SecretValue``), and its
    bytes leave only through :meth:`authorization_header`, which the MCP session
    alone calls.
    """

    __slots__ = ("__value",)
    _redacted = ACCESS_TOKEN_REDACTED

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("AccessToken wraps a string")
        self.__value: str = value

    def authorization_header(self) -> str:
        """The ``Authorization`` header value for the configured resource URL."""
        return f"Bearer {self.__value}"


# --- token requests -------------------------------------------------------------------

TOKEN_CAP_BYTES: Final = 16 * 1024
REVOCATION_UNEXPECTED: Final = "revocation_unexpected_error %s"

RevocationOutcome = Literal["revoked", "revocation_failed", "revocation_unavailable"]

_ACCESS_TOKEN: Final = re.compile(r"[\x21-\x7e]+")
_TERMINAL_ERRORS: Final[dict[str, OAuthCode]] = {
    "invalid_client": "client_rejected",
    "invalid_grant": "invalid_grant",
    "unauthorized_client": "unauthorized_client",
}
_TOKEN_HEADERS: Final = {"Accept": "application/json"}


@dataclass(frozen=True, slots=True)
class Revocation:
    """What :func:`revoke` reports; ``client_rejected`` tells the caller to clear
    the stored client."""

    outcome: RevocationOutcome
    client_rejected: bool = False


@dataclass(frozen=True, slots=True)
class _Tokens:
    access: AccessToken
    refresh: bytes | None = field(repr=False)


def _json_object(body: bytes) -> dict[str, object] | None:
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def _error_code(response: EgressResponse) -> str | None:
    """The RFC 6749 s5.2 ``error`` of a 400 or 401 JSON error response."""
    if response.status not in (400, 401):
        return None
    document = _json_object(response.body)
    error = None if document is None else document.get("error")
    return error if isinstance(error, str) else None


def _token_error(response: EgressResponse, failed: OAuthCode) -> OAuthFailure:
    """The failure for a token endpoint answer other than 200."""
    if response.status == 429:
        return OAuthFailure(
            "rate_limited", retry_after=egress.retry_after_seconds(response.headers)
        )
    error = _error_code(response)
    if error is not None and error in _TERMINAL_ERRORS:
        return OAuthFailure(_TERMINAL_ERRORS[error], terminal=True)
    return OAuthFailure(failed)


def _judge_tokens(
    response: EgressResponse, failed: OAuthCode, *, require_refresh: bool
) -> _Tokens | OAuthFailure:
    """The accepted tokens, or the failure; never raises."""
    if response.status != 200:
        return _token_error(response, failed)
    document = _json_object(response.body)
    if document is None:
        return OAuthFailure(failed)
    token_type = document.get("token_type")
    access = document.get("access_token")
    if (
        not isinstance(token_type, str)
        or token_type.lower() != "bearer"
        or not isinstance(access, str)
        or not _ACCESS_TOKEN.fullmatch(access)
    ):
        return OAuthFailure(failed)
    refresh_token = _optional_secret(document, "refresh_token")
    if isinstance(refresh_token, _Malformed):
        return OAuthFailure(failed)
    if require_refresh and refresh_token is None:
        return OAuthFailure("no_refresh_token")
    return _Tokens(access=AccessToken(access), refresh=refresh_token)


def _egress_code(code: egress.EgressCode, failed: OAuthCode) -> OAuthCode:
    if code == "endpoint_misconfigured":
        return "endpoint_misconfigured"
    if code == "too_large":
        return failed
    return "unavailable"


def _post_form(
    endpoint: str,
    form: dict[str, str],
    headers: dict[str, str],
    *,
    resolver: Resolver,
    clock: Clock,
    transport: httpx.BaseTransport | None,
) -> EgressResponse | egress.EgressCode:
    """One bounded form POST to a pinned endpoint; the response or the egress code."""
    with egress.open_client(transport) as client:
        try:
            return egress.bounded_request(
                client,
                "POST",
                endpoint,
                budget=REQUEST_BUDGET_SECONDS,
                cap=TOKEN_CAP_BYTES,
                resolver=resolver,
                clock=clock,
                headers=headers,
                data=form,
            )
        except egress.EgressFailure as failure:
            return failure.code


def _token_grant(
    endpoint: object,
    form: dict[str, str],
    *,
    client: ClientAuth,
    now: datetime | None,
    failed: OAuthCode,
    require_refresh: bool,
    resolver: Resolver,
    clock: Clock,
    transport: httpx.BaseTransport | None,
) -> _Tokens:
    """Authenticate ``form`` as ``client`` and post it to the token endpoint."""
    if not isinstance(endpoint, str) or not isinstance(client, ClientAuth):
        raise _misconfigured()
    headers = dict(_TOKEN_HEADERS)
    apply_client_authentication(
        client, form, headers, now=now if now is not None else datetime.now(UTC)
    )
    response = _post_form(
        endpoint, form, headers, resolver=resolver, clock=clock, transport=transport
    )
    if isinstance(response, str):
        raise OAuthFailure(_egress_code(response, failed))
    judged = _judge_tokens(response, failed, require_refresh=require_refresh)
    if isinstance(judged, OAuthFailure):
        raise judged
    return judged


def _read_refresh_token(ref: object, namespace: str) -> bytes | None:
    """The refresh token behind ``ref`` in ``namespace``, or ``None``."""
    if _reference_namespace(ref) != namespace or not isinstance(ref, str):
        return None
    value = credentials.store().resolve(
        SecretRef.parse(ref), _scope(namespace, writable=False)
    )
    return value.expose()


def _strings_or_refuse(*values: object) -> None:
    if not all(isinstance(v, str) for v in values):
        raise _misconfigured()


def exchange_code(
    token_endpoint: str,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    resource: str,
    client: ClientAuth,
    workspace_id: UUID,
    connection_id: UUID,
    resolver: Resolver = egress.default_resolver,
    clock: Clock = time.monotonic,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> tuple[str, AccessToken]:
    """Exchange an authorization code at the pinned token endpoint (module
    docstring); returns the new refresh token's reference and the access token."""
    _connection_namespace(workspace_id, connection_id)
    _strings_or_refuse(code, redirect_uri, code_verifier, resource)
    tokens = _token_grant(
        token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "resource": resource,
        },
        client=client,
        now=now,
        failed="exchange_failed",
        require_refresh=True,
        resolver=resolver,
        clock=clock,
        transport=transport,
    )
    refs = _write_secrets((tokens.refresh,), workspace_id, connection_id)
    if refs is None or refs[0] is None:
        raise OAuthFailure("secret_write_failed")
    return refs[0], tokens.access


def refresh(
    token_endpoint: str,
    *,
    refresh_ref: str,
    resource: str,
    client: ClientAuth,
    workspace_id: UUID,
    connection_id: UUID,
    resolver: Resolver = egress.default_resolver,
    clock: Clock = time.monotonic,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> tuple[AccessToken, str | None]:
    """Refresh at the pinned token endpoint (module docstring); returns the access
    token and the rotated refresh token's reference, or ``None`` when it did not
    rotate."""
    namespace = _connection_namespace(workspace_id, connection_id)
    _strings_or_refuse(resource)
    current: bytes | None = None
    current_text = ""
    try:
        current = _read_refresh_token(refresh_ref, namespace)
        current_text = "" if current is None else current.decode("utf-8")
    except Exception:  # a refusal's text names the path; the credential is gone
        current = None
    if current is None:
        raise OAuthFailure("secret_unavailable", terminal=True)
    tokens = _token_grant(
        token_endpoint,
        {
            "grant_type": "refresh_token",
            "refresh_token": current_text,
            "resource": resource,
        },
        client=client,
        now=now,
        failed="token_refresh_failed",
        require_refresh=False,
        resolver=resolver,
        clock=clock,
        transport=transport,
    )
    if tokens.refresh is None or hmac.compare_digest(tokens.refresh, current):
        return tokens.access, None
    refs = _write_secrets((tokens.refresh,), workspace_id, connection_id)
    if refs is None or refs[0] is None:
        raise OAuthFailure("secret_write_failed")
    return tokens.access, refs[0]


def _revoke(
    endpoint: object,
    refresh_ref: object,
    client: ClientAuth,
    workspace_id: UUID,
    connection_id: UUID,
    *,
    now: datetime | None,
    resolver: Resolver,
    clock: Clock,
    transport: httpx.BaseTransport | None,
) -> Revocation:
    if endpoint is None:
        return Revocation("revocation_unavailable")
    if (
        not isinstance(endpoint, str)
        or not isinstance(client, ClientAuth)
        or not isinstance(workspace_id, UUID)
        or not isinstance(connection_id, UUID)
    ):
        return Revocation("revocation_failed")
    form: dict[str, str] = {}
    headers = dict(_TOKEN_HEADERS)
    rejected = False
    try:
        apply_client_authentication(
            client, form, headers, now=now if now is not None else datetime.now(UTC)
        )
    except OAuthFailure:  # the expired or unreadable secret was never sent
        rejected = True
    if rejected:
        return Revocation("revocation_failed", client_rejected=True)
    token: str | None = None
    try:
        raw = _read_refresh_token(refresh_ref, _namespace(workspace_id, connection_id))
        token = None if raw is None else raw.decode("utf-8")
    except Exception:  # its text names the path; only the outcome leaves
        token = None
    if token is None:
        return Revocation("revocation_failed")
    form.update(token=token, token_type_hint="refresh_token")
    response = _post_form(
        endpoint, form, headers, resolver=resolver, clock=clock, transport=transport
    )
    if isinstance(response, str):
        return Revocation("revocation_failed")
    if response.status == 200:
        return Revocation("revoked")
    return Revocation(
        "revocation_failed", client_rejected=_error_code(response) == "invalid_client"
    )


def revoke(
    revocation_endpoint: str | None,
    *,
    refresh_ref: str,
    client: ClientAuth,
    workspace_id: UUID,
    connection_id: UUID,
    resolver: Resolver = egress.default_resolver,
    clock: Clock = time.monotonic,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> Revocation:
    """Revoke one refresh token at the pinned revocation endpoint (RFC 7009,
    module docstring). Never raises: its caller has already ended locally."""
    unexpected: str | None = None
    try:
        return _revoke(
            revocation_endpoint,
            refresh_ref,
            client,
            workspace_id,
            connection_id,
            now=now,
            resolver=resolver,
            clock=clock,
            transport=transport,
        )
    except Exception as error:  # anything at all ends as revocation_failed
        unexpected = type(error).__name__
    _log.warning(REVOCATION_UNEXPECTED, unexpected)
    return Revocation("revocation_failed")
