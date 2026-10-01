"""The OAuth connector surface's pure half (issue #287): what is advertised, and
whether anything is.

:func:`oauth_surface` reads the ``identity.oauth.*`` settings and the routing table
and returns either an :class:`OAuthSurface` (every URL the feature advertises, the
allowlist and the lifetimes) or an :class:`OAuthUnconfigured` naming why not. No
HTTP, no database.

**Every advertised URL comes from ``url_for``.** The canonical resource is
``url_for(routing, MCP, "/")`` (``https://mcp.<host>/`` in subdomain mode,
``https://<host>/mcp/`` in path mode). The issuer is that string with its one
trailing slash removed. The endpoints are ``url_for(routing, IDENTITY, "/oauth/...")``.
The two metadata URLs are derived from the canonical resource by the RFC 9728 s3.1 /
RFC 8414 s3.1 insertion rule, ``origin + "/.well-known/<name>" + path`` with the
path's trailing slash dropped, so they follow ``url_for`` in both modes too.

**Unconfigured means nothing is advertised.** ``identity.oauth.enabled`` false, the
GitHub provider off (disabled, or enabled with no client id, the rule
``identity.provider_config.sync_providers`` applies), an empty allowlist, or any
setting outside its declared bound all return :class:`OAuthUnconfigured`, as does
a routing host that makes the protected-resource URL unfit for the ``mcp`` gate's
``WWW-Authenticate`` header (not ASCII, or carrying a ``"``). The bound
is normally enforced at settings load; it is checked again here so a settings
mapping built any other way cannot advertise a broken surface.
"""

from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from rheo_core.routing import IDENTITY, MCP, RoutingConfig, RoutingMode, url_for
from rheo_core.settings import ResolvedSettings
from rheo_core.settings.schema import spec_for

ENABLED_KEY: Final = "identity.oauth.enabled"
REDIRECT_URIS_KEY: Final = "identity.oauth.redirect_uris"
ACCESS_TOKEN_MINUTES_KEY: Final = "identity.oauth.access_token_minutes"
GRANT_DAYS_KEY: Final = "identity.oauth.grant_days"
REFRESH_GRACE_SECONDS_KEY: Final = "identity.oauth.refresh_grace_seconds"
MAX_CLIENTS_KEY: Final = "identity.oauth.max_clients"
REGISTRATIONS_PER_SOURCE_KEY: Final = "identity.oauth.registrations_per_source_per_hour"
ABANDONED_CLIENT_MINUTES_KEY: Final = "identity.oauth.abandoned_client_minutes"
_INT_KEYS: Final = (
    ACCESS_TOKEN_MINUTES_KEY,
    GRANT_DAYS_KEY,
    REFRESH_GRACE_SECONDS_KEY,
    MAX_CLIENTS_KEY,
    REGISTRATIONS_PER_SOURCE_KEY,
    ABANDONED_CLIENT_MINUTES_KEY,
)
_GITHUB_ENABLED_KEY: Final = "identity.providers.github.enabled"
_GITHUB_CLIENT_ID_KEY: Final = "identity.providers.github.client_id"

PROTECTED_RESOURCE_DOCUMENT: Final = "oauth-protected-resource"
AUTHORIZATION_SERVER_DOCUMENT: Final = "oauth-authorization-server"

_LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1"})
"""The hosts a non-``https`` (``http``) redirect URI may name: a local test client."""

UnconfiguredReason = Literal[
    "disabled", "identity_provider_disabled", "allowlist_empty", "setting_invalid"
]


@dataclass(frozen=True, slots=True)
class OAuthUnconfigured:
    """The feature advertises nothing; ``reason`` says why (doctor reports it)."""

    reason: UnconfiguredReason


@dataclass(frozen=True, slots=True)
class OAuthLifetimes:
    access_token_minutes: int
    grant_days: int
    refresh_grace_seconds: int
    max_clients: int
    registrations_per_source_per_hour: int
    abandoned_client_minutes: int


@dataclass(frozen=True, slots=True)
class OAuthSurface:
    """Everything the configured feature advertises."""

    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    protected_resource_metadata_url: str
    authorization_server_metadata_url: str
    redirect_uris: tuple[str, ...]
    lifetimes: OAuthLifetimes
    accepts_empty_path: bool
    """Subdomain mode with a root resource path: RFC 3986 s6.2.3 makes
    ``https://mcp.host`` and ``https://mcp.host/`` one URI, so both match."""

    def protected_resource_metadata(self) -> dict[str, object]:
        """The RFC 9728 document."""
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "bearer_methods_supported": ["header"],
        }

    def authorization_server_metadata(self) -> dict[str, object]:
        """The RFC 8414 document: exactly these fields. No
        ``client_id_metadata_document_supported`` (DCR only) and no
        ``scopes_supported`` (no scope vocabulary in v1)."""
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "registration_endpoint": self.registration_endpoint,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True,
        }

    def resource_matches(self, value: str) -> bool:
        """Exact string match against the canonical resource, plus the empty-path
        spelling in subdomain mode. Every other variant (path mode's ``/mcp`` for
        ``/mcp/``, an extra path, a case change, a query) is refused."""
        if value == self.resource:
            return True
        return self.accepts_empty_path and value == self.issuer


def _well_known(resource: str, name: str) -> str:
    """RFC 9728 s3.1 / RFC 8414 s3.1: insert ``/.well-known/<name>`` between the
    origin and the path, dropping the path's trailing slash."""
    parts = urlsplit(resource)
    return f"{parts.scheme}://{parts.netloc}/.well-known/{name}{parts.path.rstrip('/')}"


def _redirect_uri_valid(uri: str) -> bool:
    """``https`` with a host, or ``http`` on a loopback host, judged on the parsed
    URI: a prefix test would pass ``http://localhost:x@other.example/``. No
    userinfo, no fragment (RFC 6749 s3.1.2), and a port must parse."""
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return False
    if "@" in parts.netloc or parts.fragment or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS


def _header_safe(url: str) -> bool:
    """Whether ``url`` can sit inside the ``WWW-Authenticate`` quoted string the
    ``mcp`` gate sends: ASCII-encodable and free of ``"``. A routing host that makes
    the pointer fail this would otherwise break the 401 for every caller, so the
    surface is refused instead and the gate keeps its bare ``Bearer``."""
    return url.isascii() and '"' not in url


def _allowlist_valid(uris: list[str]) -> bool:
    return all(_redirect_uri_valid(uri) for uri in uris)


def _ints_in_bounds(settings: ResolvedSettings) -> bool:
    for key in _INT_KEYS:
        spec = spec_for(key)
        value = settings.get_int(key)
        if spec.minimum is not None and value < spec.minimum:
            return False
        if spec.maximum is not None and value > spec.maximum:
            return False
    return True


def oauth_surface(
    settings: ResolvedSettings, routing: RoutingConfig
) -> OAuthSurface | OAuthUnconfigured:
    """The configured surface, or why there is none (module docstring)."""
    if not settings.get_bool(ENABLED_KEY):
        return OAuthUnconfigured("disabled")
    if not settings.get_bool(_GITHUB_ENABLED_KEY) or not settings.get_str(
        _GITHUB_CLIENT_ID_KEY
    ):
        return OAuthUnconfigured("identity_provider_disabled")
    redirect_uris = settings.get_list(REDIRECT_URIS_KEY)
    if not redirect_uris:
        return OAuthUnconfigured("allowlist_empty")
    if not _allowlist_valid(redirect_uris) or not _ints_in_bounds(settings):
        return OAuthUnconfigured("setting_invalid")
    resource = url_for(routing, MCP, "/")
    issuer = resource.removesuffix("/")
    protected_resource_metadata_url = _well_known(resource, PROTECTED_RESOURCE_DOCUMENT)
    if not _header_safe(protected_resource_metadata_url):
        return OAuthUnconfigured("setting_invalid")
    return OAuthSurface(
        resource=resource,
        issuer=issuer,
        authorization_endpoint=url_for(routing, IDENTITY, "/oauth/authorize"),
        token_endpoint=url_for(routing, IDENTITY, "/oauth/token"),
        registration_endpoint=url_for(routing, IDENTITY, "/oauth/register"),
        protected_resource_metadata_url=protected_resource_metadata_url,
        authorization_server_metadata_url=_well_known(
            resource, AUTHORIZATION_SERVER_DOCUMENT
        ),
        redirect_uris=tuple(redirect_uris),
        lifetimes=OAuthLifetimes(
            access_token_minutes=settings.get_int(ACCESS_TOKEN_MINUTES_KEY),
            grant_days=settings.get_int(GRANT_DAYS_KEY),
            refresh_grace_seconds=settings.get_int(REFRESH_GRACE_SECONDS_KEY),
            max_clients=settings.get_int(MAX_CLIENTS_KEY),
            registrations_per_source_per_hour=settings.get_int(
                REGISTRATIONS_PER_SOURCE_KEY
            ),
            abandoned_client_minutes=settings.get_int(ABANDONED_CLIENT_MINUTES_KEY),
        ),
        accepts_empty_path=routing.mode is RoutingMode.SUBDOMAIN
        and urlsplit(resource).path == "/",
    )
