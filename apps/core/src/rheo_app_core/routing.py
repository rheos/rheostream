"""This deployment's routing configuration, module surfaces included, in one place.

Three call sites in this app need it: the ``/auth/*`` routes (the ``invalid_return``
and ``Origin`` checks), the internal listener's routing endpoint, and the ``mcp``
mount. Each used to build :class:`~rheo_core.routing.RoutingConfig` itself, and the
identity routes were the one that forgot the modules. In subdomain mode that left
every module host out of core's application-host set, so a signed-out visitor to a
module's screens was refused ``invalid_return`` at ``/auth/continue`` and could never
sign in (issue #119). One helper means one conversion to get right.

The ``modules`` surfaces are not settings (no key backs them, D-6): they are whatever
the modules this process loaded declared, which ``rheo_core.modules.module_surfaces()``
kept from startup. The ``WebSurface -> SurfaceConfig`` conversion happens here, at the
point of use, so ``rheo_core.modules`` never gains a dependency on
``rheo_core.routing``. A deployment that loaded no module gets an empty mapping.

Resolved fresh on every call, never cached: an operator's routing change takes
effect on the very next request.
"""

from rheo_core.modules import module_surfaces
from rheo_core.routing import RoutingConfig, SurfaceConfig
from rheo_core.settings import resolve


def routing_config() -> RoutingConfig:
    """The current routing configuration, with every loaded module surface."""
    modules = {
        surface: SurfaceConfig(host=web.host, path=web.path)
        for surface, web in module_surfaces().items()
    }
    return RoutingConfig.from_settings(resolve(), modules=modules)
