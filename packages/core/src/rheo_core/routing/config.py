"""The routing configuration (D7, FR 47): the URL topology as one typed object.

One ``RoutingConfig`` per deployment, built from the ``routing.*`` deployment settings
and served to the web tier through the internal API. It is the only place a host or a
topology-specific prefix is named: every link, redirect, callback URL and MCP
configuration file goes through :func:`~rheo_core.routing.url_for.url_for` rather than
spelling one out.

Pydantic rather than a frozen dataclass because this object crosses the validation
boundary in both directions — the internal listener serialises it to the web tier, and
the shared fixture (``tests/fixtures/routing/*.json``) is deserialised back into one so
the Python and TypeScript implementations are proven against the same bytes. That is
the house split already in the tree: payloads crossing a boundary are frozen
``BaseModel``\\ s, internal plumbing is a frozen dataclass.

``identity`` is the one surface whose path prefix survives in subdomain mode
(``fixed_path``), because the routing table sends ``/auth/*`` on **every** application
host to ``core`` in both modes. It is hardcoded below rather than read from a setting:
an operator who relocated it would break that invariant.

``modules`` is not settings-driven and never will be: a module surface is named by the
manifest of whatever module a deployment loaded, so :meth:`RoutingConfig.from_settings`
takes it as an optional argument (default empty, reproducing a module-less deployment
exactly) and the internal listener passes what ``rheo_core.modules.module_surfaces()``
kept from startup (run 0v, D-6).
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from rheo_core.settings import ResolvedSettings


class RoutingMode(StrEnum):
    """The two topologies. ``path`` is what a fresh install produces."""

    PATH = "path"
    SUBDOMAIN = "subdomain"


SHELL: Final = "shell"
IDENTITY: Final = "identity"
API: Final = "api"
MCP: Final = "mcp"
DOCS: Final = "docs"
INTEGRATION: Final = "integration"

NAMED_SURFACES: Final[tuple[str, ...]] = (SHELL, IDENTITY, API, MCP, DOCS, INTEGRATION)
"""The surfaces every deployment has. Module surfaces are named at runtime."""

KEY_PREFIX: Final = "routing"
MODE_KEY: Final = f"{KEY_PREFIX}.mode"
SCHEME_KEY: Final = f"{KEY_PREFIX}.scheme"
BASE_HOST_KEY: Final = f"{KEY_PREFIX}.base_host"


def surface_key(surface: str, field: str) -> str:
    """The settings key for one surface's field (``routing.shell.host``)."""
    return f"{KEY_PREFIX}.{surface}.{field}"


class SurfaceConfig(BaseModel):
    """One surface's host label and path prefix.

    ``path`` defaults to the empty string because the two surfaces this application
    never links to — ``docs`` and ``integration`` — declare no path key at all.
    ``external`` means "not this application"; ``reserved`` means "not this
    application, and nothing serves it in release one". Either one makes ``url_for``
    refuse.
    """

    model_config = ConfigDict(frozen=True)

    host: str
    path: str = ""
    external: bool = False
    reserved: bool = False
    fixed_path: bool = False


class Surfaces(BaseModel):
    """The routing table's surfaces, keyed the way the shared fixture spells them."""

    model_config = ConfigDict(frozen=True)

    shell: SurfaceConfig
    identity: SurfaceConfig
    api: SurfaceConfig
    mcp: SurfaceConfig
    docs: SurfaceConfig
    integration: SurfaceConfig
    modules: Mapping[str, SurfaceConfig] = {}

    def named(self) -> Mapping[str, SurfaceConfig]:
        """The six always-present surfaces, by name."""
        return {
            SHELL: self.shell,
            IDENTITY: self.identity,
            API: self.api,
            MCP: self.mcp,
            DOCS: self.docs,
            INTEGRATION: self.integration,
        }

    def get(self, name: str) -> SurfaceConfig | None:
        """The surface called ``name``, or ``None``. A named surface wins over a
        module that happens to share its name."""
        surface = self.named().get(name)
        if surface is not None:
            return surface
        return self.modules.get(name)


class RoutingConfig(BaseModel):
    """The whole topology: the mode, the scheme, the base host, and the surfaces."""

    model_config = ConfigDict(frozen=True)

    mode: RoutingMode
    scheme: str
    base_host: str
    surfaces: Surfaces

    @classmethod
    def from_settings(
        cls,
        settings: ResolvedSettings,
        *,
        modules: Mapping[str, SurfaceConfig] = {},
    ) -> "RoutingConfig":
        """Build the configuration from the sixteen resolved ``routing.*`` keys.

        No database and no secret store: the routing configuration is deployment
        settings and nothing else.

        ``modules`` is the one part that does not come from settings — no key backs
        it, because the surfaces a deployment has are the ones its loaded modules
        declare (D-6). The default is empty and reproduces a module-less deployment
        byte for byte, which is what keeps ``tests/fixtures/routing/*.json`` and
        every shipped ``from_settings`` caller unchanged. The keyword is read
        keyword-only so a positional second argument can never be mistaken for one
        of the settings.
        """
        return cls(
            mode=RoutingMode(settings.get_str(MODE_KEY)),
            scheme=settings.get_str(SCHEME_KEY),
            base_host=settings.get_str(BASE_HOST_KEY),
            surfaces=Surfaces(
                shell=SurfaceConfig(
                    host=settings.get_str(surface_key(SHELL, "host")),
                    path=settings.get_str(surface_key(SHELL, "path")),
                ),
                # fixed_path is not a setting: see the module docstring.
                identity=SurfaceConfig(
                    host=settings.get_str(surface_key(IDENTITY, "host")),
                    path=settings.get_str(surface_key(IDENTITY, "path")),
                    fixed_path=True,
                ),
                api=SurfaceConfig(
                    host=settings.get_str(surface_key(API, "host")),
                    path=settings.get_str(surface_key(API, "path")),
                ),
                mcp=SurfaceConfig(
                    host=settings.get_str(surface_key(MCP, "host")),
                    path=settings.get_str(surface_key(MCP, "path")),
                ),
                docs=SurfaceConfig(
                    host=settings.get_str(surface_key(DOCS, "host")),
                    external=settings.get_bool(surface_key(DOCS, "external")),
                ),
                integration=SurfaceConfig(
                    host=settings.get_str(surface_key(INTEGRATION, "host")),
                    external=settings.get_bool(surface_key(INTEGRATION, "external")),
                    reserved=settings.get_bool(surface_key(INTEGRATION, "reserved")),
                ),
                modules=dict(modules),
            ),
        )
