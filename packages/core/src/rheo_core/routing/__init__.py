"""Routing configuration (the host/path routing table) and the URL builders (D7,
FR 47).

``config.py`` holds the typed configuration loaded from the ``routing.*`` deployment
settings; ``url_for.py`` holds the four pure functions every link, redirect and
callback URL goes through. The web tier implements the same rule in TypeScript, and
both are proven against one shared fixture (``tests/fixtures/routing/*.json``).
"""

from rheo_core.routing.config import (
    API,
    BASE_HOST_KEY,
    DOCS,
    IDENTITY,
    INTEGRATION,
    KEY_PREFIX,
    MCP,
    MODE_KEY,
    NAMED_SURFACES,
    PUBLIC_HOST_KEY,
    SCHEME_KEY,
    SHELL,
    RoutingConfig,
    RoutingMode,
    SurfaceConfig,
    Surfaces,
    surface_key,
)
from rheo_core.routing.url_for import (
    application_hosts,
    identity_path,
    is_application_host,
    url_for,
)

__all__ = [
    "API",
    "BASE_HOST_KEY",
    "DOCS",
    "IDENTITY",
    "INTEGRATION",
    "KEY_PREFIX",
    "MCP",
    "MODE_KEY",
    "NAMED_SURFACES",
    "PUBLIC_HOST_KEY",
    "SCHEME_KEY",
    "SHELL",
    "RoutingConfig",
    "RoutingMode",
    "SurfaceConfig",
    "Surfaces",
    "application_hosts",
    "identity_path",
    "is_application_host",
    "surface_key",
    "url_for",
]
