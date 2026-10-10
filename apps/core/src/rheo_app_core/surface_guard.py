"""Core's own host and prefix rules for the ``api`` and identity surfaces (issue #290).

Two rules the reverse proxy used to carry alone, now held by ``core`` as well.

**``/api/*`` is served on the ``api`` host only, in subdomain mode.** The HTTP API's
routes (``api_routes.API_PREFIX``, the operations route and any declared connector
route) answer on ``<routing.api.host>.<base_host>``. A request for ``/api`` or
anything under it on any other host (the shell, the identity host, a module host,
the bare base host) answers 404 here, before FastAPI, so separating the api host
from the application hosts no longer depends on the proxy's rules alone. The api
host itself is unchanged: it still serves ``/healthz`` and whatever else it served.
Path mode has one host, so ``/api/*`` is served on whatever host the request names,
as the ``mcp`` surface is (``mcp_mount``).

**The identity routes are mounted at ``routing.identity.path``.** ``auth_routes`` and
``oauth_routes`` register their handlers under the declared default prefix
(:data:`~rheo_core.oauth.surface.SERVED_IDENTITY_PATH`, ``/auth``), while
``url_for(IDENTITY, ...)`` and ``identity_path`` build every advertised URL under the
configured one. With the default, nothing changes. With another prefix, this guard
rewrites ``<prefix>`` and ``<prefix>/...`` onto the served ``/auth`` paths (in a copy
of the scope) and answers the served paths themselves 404, so the URLs core
advertises are exactly the ones it answers, in both modes. The OAuth connector stays
unconfigured under a non-default prefix (``rheo_core.oauth.surface``); its rewritten
paths reach routes that 404 for that reason, as they did before.

An identity prefix that overlaps another route ``core`` answers (``/api``,
``/healthz``, ``/.well-known``, or the ``mcp`` surface's path in path mode) refuses
startup: the rewrite would otherwise take that route away silently.

**Built in the lifespan, like the ``mcp`` mount.** :func:`guarded_surfaces` builds
the rules from the resolved routing settings after startup has checked them and
publishes them on ``app.state``; a settings change takes effect at restart, as every
``RHEO__*`` change does. Outside a lifespan there are no rules and the guard passes
every request through, which keeps the database-free tests that drive ``app``
without a lifespan exactly as they were.
"""

import contextlib
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Final

from fastapi import FastAPI
from rheo_core.oauth.surface import SERVED_IDENTITY_PATH
from rheo_core.routing import RoutingConfig, RoutingMode
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from rheo_app_core.api_routes import API_PREFIX
from rheo_app_core.auth_routes import normalize_host
from rheo_app_core.routing import routing_config

STATE_ATTRIBUTE: Final = "surface_rules"
"""The ``app.state`` attribute :func:`guarded_surfaces` publishes the rules under."""

HEALTHZ_PATH: Final = "/healthz"
WELL_KNOWN_PREFIX: Final = "/.well-known"

_HOST_HEADER: Final = b"host"


def under(path: str, prefix: str) -> bool:
    """Whether ``path`` is ``prefix`` itself or a path below it (segment-wise, so
    ``/apis`` is not under ``/api``)."""
    return path == prefix or path.startswith(f"{prefix}/")


def _overlaps(first: str, second: str) -> bool:
    return under(first, second) or under(second, first)


@dataclass(frozen=True, slots=True)
class SurfaceRules:
    """One lifespan's rules.

    ``api_host`` is the port-free ``api`` host in subdomain mode and ``None`` in path
    mode (no host check). ``identity_prefix`` is ``routing.identity.path``, already
    canonical (``RoutingConfig.from_settings`` checked it).
    """

    api_host: str | None
    identity_prefix: str

    @classmethod
    def for_config(cls, config: RoutingConfig) -> "SurfaceRules":
        prefix = config.surfaces.identity.path
        if prefix != SERVED_IDENTITY_PATH:
            taken = [API_PREFIX, HEALTHZ_PATH, WELL_KNOWN_PREFIX]
            if config.mode is RoutingMode.PATH:
                taken.append(config.surfaces.mcp.path)
            for route in taken:
                if _overlaps(prefix, route):
                    raise ValueError(
                        f"routing.identity.path {prefix!r} overlaps {route!r}, which "
                        "core already serves; choose a prefix outside it"
                    )
        if config.mode is RoutingMode.PATH:
            return cls(api_host=None, identity_prefix=prefix)
        label = config.surfaces.api.host.strip()
        if not label:
            raise ValueError("routing.api.host must name a label in subdomain mode")
        return cls(
            api_host=normalize_host(f"{label}.{config.base_host}"),
            identity_prefix=prefix,
        )

    def refuses(self, scope: Scope) -> bool:
        """Whether this request is ``/api/*`` on a host that is not the api host."""
        if self.api_host is None:
            return False
        if not under(scope.get("path", ""), API_PREFIX):
            return False
        return _request_host(scope) != self.api_host

    def identity_target(self, path: str) -> str | None:
        """The served path for ``path`` under a relocated identity prefix, ``""``
        for a served ``/auth`` path that is not reachable under it (a 404), and
        ``None`` when the identity rule leaves the request alone."""
        prefix = self.identity_prefix
        if prefix == SERVED_IDENTITY_PATH:
            return None
        if under(path, prefix):
            return SERVED_IDENTITY_PATH + path[len(prefix) :]
        if under(path, SERVED_IDENTITY_PATH):
            return ""
        return None


def _request_host(scope: Scope) -> str:
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", ())
    for key, value in headers:
        if key.lower() == _HOST_HEADER:
            return normalize_host(value.decode("latin-1"))
    return ""


def current_rules(scope: Scope) -> SurfaceRules | None:
    """The rules the running lifespan published, or ``None`` outside one."""
    app = scope.get("app")
    state = getattr(app, "state", None)
    rules = getattr(state, STATE_ATTRIBUTE, None)
    return rules if isinstance(rules, SurfaceRules) else None


def _rewritten(scope: Scope, target: str, prefix: str) -> Scope:
    """A copy of ``scope`` whose path is ``target``. ``raw_path`` keeps the request's
    own encoding of the tail when its raw form starts with the (unreserved-only)
    prefix, and is the encoded target otherwise."""
    forwarded = dict(scope)
    forwarded["path"] = target
    raw: bytes | None = scope.get("raw_path")
    encoded_prefix = prefix.encode("ascii")
    if raw is not None and raw.startswith(encoded_prefix):
        forwarded["raw_path"] = (
            SERVED_IDENTITY_PATH.encode("ascii") + raw[len(encoded_prefix) :]
        )
    else:
        forwarded["raw_path"] = target.encode("utf-8")
    return forwarded


class SurfaceGuard:
    """Pure ASGI middleware applying :class:`SurfaceRules`; everything else onward.

    Pure ASGI rather than ``BaseHTTPMiddleware`` for the reason ``McpSurfaceRouter``
    gives: no buffering, and no second request object in front of the routes.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        rules = current_rules(scope) if scope["type"] == "http" else None
        if rules is None:
            await self.app(scope, receive, send)
            return
        if rules.refuses(scope):
            await _not_found(scope, receive, send)
            return
        target = rules.identity_target(scope.get("path", ""))
        if target is None:
            await self.app(scope, receive, send)
            return
        if not target:
            await _not_found(scope, receive, send)
            return
        await self.app(_rewritten(scope, target, rules.identity_prefix), receive, send)


async def _not_found(scope: Scope, receive: Receive, send: Send) -> None:
    # The body FastAPI gives an unrouted path, so a refused request looks like one.
    await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)


@contextlib.asynccontextmanager
async def guarded_surfaces(app: FastAPI) -> AsyncIterator[SurfaceRules]:
    """Build the rules from the resolved routing settings and publish them.

    Raises ``ValueError`` for an identity prefix that overlaps another core route,
    which fails the lifespan and so the process's startup.
    """
    rules = SurfaceRules.for_config(routing_config())
    setattr(app.state, STATE_ATTRIBUTE, rules)
    try:
        yield rules
    finally:
        setattr(app.state, STATE_ATTRIBUTE, None)


__all__ = [
    "STATE_ATTRIBUTE",
    "SurfaceGuard",
    "SurfaceRules",
    "current_rules",
    "guarded_surfaces",
    "under",
]
