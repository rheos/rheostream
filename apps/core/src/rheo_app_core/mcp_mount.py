"""The ``mcp`` surface, mounted in the ``core`` process (issue #127).

``runtime-and-mcp.md`` puts the MCP facade in this process "at the ``mcp`` surface
(subdomain mode ``mcp.<base>/``; path mode ``/mcp``)", and the runtime writes the
CLI's MCP configuration as ``url_for(config, MCP, "/")``. This module is what answers
that URL: :class:`McpSurfaceRouter` sits in front of ``public_app`` and sends the
surface's requests to the gated application ``rheo_app_mcp.transport`` builds, and
every other request on to FastAPI unchanged.

**Which requests are the surface.**

- *Path mode*: exactly the surface path and that path with a trailing slash
  (``/mcp`` and ``/mcp/`` by default), on whatever host the request names.
  ``url_for`` yields ``/mcp/`` and a hand-written client configuration is as likely
  to say ``/mcp``; both reach the one SDK route without a redirect, because a 307
  in answer to a JSON-RPC ``POST`` is a failure many clients do not follow. Longer
  paths under the prefix are not the surface and fall through to FastAPI (a 404).
  The ``Host`` check is the SDK's DNS-rebinding protection, below.
- *Subdomain mode*: every request whose ``Host`` (port and trailing dots stripped,
  lowercased: ``auth_routes.normalize_host``) is the ``mcp`` host. ``/`` is the
  endpoint; any other path on that host is a plain 404 answered here. **The ``mcp``
  host serves MCP and nothing else**: not ``/auth/*`` (the ``mcp`` host is not an
  application host, ``routing.url_for.application_hosts``, so no session cookie or
  grant belongs on it), not ``/api/*``, not ``/healthz``. A proxy that routed
  ``mcp.<base>`` to this process before this change would have reached all of those;
  now it reaches the endpoint alone. Requests on every other host never reach MCP,
  whatever their path, so ``circuit.<base>/mcp`` goes to FastAPI. The label is
  checked at mount time: an empty one, or one whose host is also an application host
  or the ``api`` host, refuses startup rather than letting this router shadow that
  host's routes.

**Only ``POST`` reaches the façade.** The server is stateless
(``rheo_app_mcp.transport``'s docstring says why), so there is no session to open a
``GET`` event stream on or to ``DELETE``; the router answers both, and every other
method, ``405`` with ``Allow: POST``, which the streamable-HTTP specification names
as the answer for a server that offers no ``GET`` stream. The SDK client opens no
``GET`` stream when it was issued no session id, so nothing a client needs is lost.

**Why a router in front, and not ``app.mount``.** A Starlette ``Mount`` matches a
path prefix only, so it cannot express the subdomain rule, and it hands the child a
``root_path`` the SDK's single route would then have to account for. The router
rewrites the matched endpoint's path to :data:`~rheo_app_mcp.transport.MCP_PATH` in
a copy of the scope and calls the gated application directly, so the SDK sees one
route in both modes and the bearer gate stays the first thing any MCP request meets.

**Why it is built in the lifespan, not at import.** Two reasons, both hard. The
SDK's session manager (stateless, but still the owner of the per-request task group)
can be ``run()`` once per instance, and ``tests/`` runs this process's lifespan more
than once; and the routing configuration comes from the deployment's resolved
settings (``RHEO__routing__*``), which the lifespan is the first point to read after
startup has checked them. So :func:`mounted_mcp` builds a fresh application, runs its
session manager for the lifespan's duration (a mounted sub-application's own lifespan
never runs, which is the SDK's documented trap), and publishes it on ``app.state``.
Before the lifespan starts and after it ends there is no mount, and the router passes
everything through: ``tests/test_healthz.py`` and ``tests/test_git_clean.py`` drive
``app`` with no lifespan and no database, and ``/mcp`` there is FastAPI's ordinary
404.

**The DNS-rebinding allow-list is the surface's own hosts, exactly.** Path mode:
``base_host``, plus ``public_host`` when it differs (it carries a port, say
``example.test:8443``). Subdomain mode: ``mcp.<base_host>``, plus
``mcp.<public_host>`` when that differs. Origins are the same authorities under
``routing.scheme``. No wildcard port is admitted for a real name, so a deployment
whose clients reach it on a port must say so in ``routing.public_host``, which is also
what makes ``url_for`` print a URL those clients can use.

**Except on loopback.** When ``base_host`` is ``localhost`` or ``127.0.0.1`` the list
also admits ``localhost:*``, ``127.0.0.1:*`` and ``[::1]:*`` (and, in subdomain mode,
``mcp.<base_host>:*``), with origins under both ``http`` and ``https``: the SDK's own
default for a loopback server. That is safe because a DNS-rebinding attack presents
the attacker's own domain in ``Host``, never a loopback name, and it is what lets a
local demo reached on ``localhost:8000`` work without spelling the port out. Protection
is never switched off (``transport.build_mcp_app`` refuses a settings object that
would).
"""

import contextlib
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Final

from fastapi import FastAPI
from rheo_app_mcp.transport import MCP_PATH, build_mcp_app, transport_security_for
from rheo_core.modules import module_surfaces
from rheo_core.routing import RoutingConfig, RoutingMode, SurfaceConfig
from rheo_core.routing.url_for import ROOT_PREFIXES, application_hosts
from rheo_core.settings import resolve
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from rheo_app_core.auth_routes import normalize_host
from rheo_app_core.startup import CONSUMERS

STATE_ATTRIBUTE: Final = "mcp_mount"
"""The ``app.state`` attribute :func:`mounted_mcp` publishes the live mount under."""

LOOPBACK_BASE_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1"})
"""The ``base_host`` values that turn on the loopback wildcard. ``::1`` cannot be a
``base_host`` at all (``RoutingConfig`` refuses a colon in it), so it appears only in
the wildcard list below, spelled as a ``Host`` header spells it."""

LOOPBACK_WILDCARD_HOSTS: Final[tuple[str, ...]] = (
    "localhost:*",
    "127.0.0.1:*",
    "[::1]:*",
)
"""Verbatim the SDK's own loopback default (``streamable_http_app``)."""

ALLOWED_METHOD: Final = "POST"

_HOST_HEADER: Final = b"host"


def _is_loopback(config: RoutingConfig) -> bool:
    return config.base_host in LOOPBACK_BASE_HOSTS


def mcp_hosts(config: RoutingConfig) -> tuple[str, ...]:
    """The ``Host`` header values the ``mcp`` surface admits, for the allow-list.

    ``base_host`` is port-free by construction (``RoutingConfig``'s validator) and
    ``public_host`` inherits it when empty, so the exact pair is one name for a
    default-port deployment and two (bare and ``:port``) for one that publishes a
    port. The loopback wildcards follow when ``base_host`` is loopback. Ordered and
    de-duplicated so the list reads the same every time.
    """
    names: list[str]
    if config.mode is RoutingMode.PATH:
        names = [config.base_host, config.public_host]
        if _is_loopback(config):
            names.extend(LOOPBACK_WILDCARD_HOSTS)
    else:
        label = config.surfaces.mcp.host
        names = [f"{label}.{config.base_host}", f"{label}.{config.public_host}"]
        if _is_loopback(config):
            names.append(f"{label}.{config.base_host}:*")
    return tuple(dict.fromkeys(names))


def mcp_origins(config: RoutingConfig) -> tuple[str, ...]:
    """The ``Origin`` values admitted: :func:`mcp_hosts` under ``routing.scheme``,
    or under both ``http`` and ``https`` on loopback, as the SDK's default does."""
    schemes = ("http", "https") if _is_loopback(config) else (config.scheme,)
    return tuple(
        dict.fromkeys(
            f"{scheme}://{host}" for host in mcp_hosts(config) for scheme in schemes
        )
    )


def build_mcp_surface(config: RoutingConfig) -> Starlette:
    """The MCP façade, wired to **this** process's one ``ConsumerRegistry``.

    ``build_mcp_app`` takes ``consumers`` as a keyword with no default, so a caller
    that forgot it would not type-check rather than quietly publishing into a
    registry nobody subscribed to; naming :data:`~rheo_app_core.startup.CONSUMERS`
    here is what makes a tool call over MCP and an HTTP dispatch reach the same
    object. The allow-list is :func:`mcp_hosts` and :func:`mcp_origins`.

    ``json_response=True``: each ``POST`` is answered with one JSON body rather than
    a one-message event stream. Stateless, there is nothing a stream could carry
    after the reply, and a plain body is the smallest thing a proxy can mishandle.
    """
    return build_mcp_app(
        consumers=CONSUMERS,
        path=MCP_PATH,
        json_response=True,
        transport_security=transport_security_for(
            mcp_hosts(config), origins=mcp_origins(config)
        ),
    )


def routing_config() -> RoutingConfig:
    """This deployment's routing configuration, module surfaces included.

    The modules are the ones startup loaded, converted exactly as
    ``internal_routes.routing_config`` converts them, so the collision check in
    :meth:`McpMount.for_config` sees every host the web tier will serve.
    """
    modules = {
        surface: SurfaceConfig(host=web.host, path=web.path)
        for surface, web in module_surfaces().items()
    }
    return RoutingConfig.from_settings(resolve(), modules=modules)


@dataclass(frozen=True, slots=True)
class McpMount:
    """One lifespan's mounted surface: the rule for matching it and the app behind it.

    ``host`` is the port-free ``mcp`` host in subdomain mode and ``None`` in path
    mode; ``path`` is the surface path in path mode and ``/`` in subdomain mode.
    """

    app: ASGIApp
    host: str | None
    path: str

    @classmethod
    def for_config(cls, config: RoutingConfig, app: ASGIApp) -> "McpMount":
        if config.mode is RoutingMode.PATH:
            path = config.surfaces.mcp.path
            if path in ROOT_PREFIXES or not path.startswith("/") or path.endswith("/"):
                # A root surface path would claim every request on the one host,
                # the shell and ``/auth/*`` included; a relative or slash-ended one
                # is a typo ``url_for`` would turn into a wrong URL. Refused loudly
                # at startup rather than served strangely.
                raise ValueError(
                    f"routing.mcp.path must be a non-root absolute path without a "
                    f"trailing slash in path mode, not {path!r}"
                )
            return cls(app=app, host=None, path=path)
        label = config.surfaces.mcp.host.strip()
        if not label:
            raise ValueError("routing.mcp.host must name a label in subdomain mode")
        host = normalize_host(f"{label}.{config.base_host}")
        taken = set(application_hosts(config))
        taken.add(normalize_host(f"{config.surfaces.api.host}.{config.base_host}"))
        if host in taken:
            # The router claims every request on the mcp host, so a label shared
            # with the shell, the identity host, the api host or a module host would
            # silently take that host's routes away from it.
            raise ValueError(
                f"routing.mcp.host {label!r} makes the mcp host {host!r}, which is "
                "already an application host or the api host"
            )
        return cls(app=app, host=host, path="/")

    def claims(self, scope: Scope) -> bool | None:
        """``True`` for the endpoint, ``False`` for another path on the ``mcp``
        host (a 404 here), ``None`` for a request that is not this surface's."""
        request_path: str = scope.get("path", "")
        if self.host is None:
            return True if request_path in (self.path, f"{self.path}/") else None
        if _request_host(scope) != self.host:
            return None
        return request_path in ("", "/")


def _request_host(scope: Scope) -> str:
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", ())
    for key, value in headers:
        if key.lower() == _HOST_HEADER:
            return normalize_host(value.decode("latin-1"))
    return ""


def current_mount(scope: Scope) -> McpMount | None:
    """The mount the running lifespan published, or ``None`` outside one."""
    app = scope.get("app")
    state = getattr(app, "state", None)
    mount = getattr(state, STATE_ATTRIBUTE, None)
    return mount if isinstance(mount, McpMount) else None


class McpSurfaceRouter:
    """Pure ASGI middleware: the ``mcp`` surface to MCP, everything else onward.

    Pure ASGI rather than ``BaseHTTPMiddleware``, which would buffer every response
    it passes and put a second request object in front of the bearer gate.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        mount = current_mount(scope) if scope["type"] == "http" else None
        claimed = None if mount is None else mount.claims(scope)
        if mount is None or claimed is None:
            await self.app(scope, receive, send)
            return
        if not claimed:
            await JSONResponse({"detail": "Not Found"}, status_code=404)(
                scope, receive, send
            )
            return
        if scope.get("method") != ALLOWED_METHOD:
            await JSONResponse(
                {"detail": "Method Not Allowed"},
                status_code=405,
                headers={"Allow": ALLOWED_METHOD},
            )(scope, receive, send)
            return
        forwarded = dict(scope)
        forwarded["path"] = MCP_PATH
        forwarded["raw_path"] = MCP_PATH.encode("ascii")
        await mount.app(forwarded, receive, send)


@contextlib.asynccontextmanager
async def mounted_mcp(app: FastAPI) -> AsyncIterator[McpMount]:
    """Build the surface from the resolved settings, run it, and publish it.

    Entered by ``main.lifespan`` after ``run_startup`` and exited before the storage
    engines are disposed, so no MCP request is still dispatching when its database
    connections go.
    """
    config = routing_config()
    surface = build_mcp_surface(config)
    mount = McpMount.for_config(config, surface)
    async with surface.router.lifespan_context(surface):
        setattr(app.state, STATE_ATTRIBUTE, mount)
        try:
            yield mount
        finally:
            setattr(app.state, STATE_ATTRIBUTE, None)


__all__ = [
    "ALLOWED_METHOD",
    "LOOPBACK_WILDCARD_HOSTS",
    "STATE_ATTRIBUTE",
    "McpMount",
    "McpSurfaceRouter",
    "build_mcp_surface",
    "current_mount",
    "mcp_hosts",
    "mcp_origins",
    "mounted_mcp",
    "routing_config",
]
