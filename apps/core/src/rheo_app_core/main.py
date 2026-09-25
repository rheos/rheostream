"""FastAPI composition root for the ``core`` process.

The module-level name ``app`` is the object ``tests/test_healthz.py`` and
``tests/test_git_clean.py`` import; it stays bound here, built with the ``lifespan``
below. The lifespan is an async context manager that runs ``startup.run_startup``
once (off the event loop, it blocks on the database) between startup and shutdown,
and disposes the storage backend's engines on the way out. ``GET /healthz`` stays a
liveness probe with no database call.

``httpx``'s ASGI transport does not run ``lifespan``, which is why the 0a healthz
tests stay database-free and why ``tests/postgres/test_cli.py`` drives the lifespan
explicitly to prove the startup sequence.

The ``/auth/*`` routes, ``/api/v1/operations``, the internal listener, ``serve()``
and the MCP facade seam are 0b2's; the MCP transport is 0c's. The ``mcp`` surface is
served by this app too (issue #127): :class:`~rheo_app_core.mcp_mount.McpSurfaceRouter`
sits in front of the routes below and hands the surface's requests to the façade,
and the lifespan builds that façade from the resolved routing settings and runs its
session manager (``mcp_mount.py`` has the matching rules and the reasons). With no
lifespan running there is no mount, so the database-free tests above see only
FastAPI.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from rheo_contracts import CONTRACT_VERSION
from rheo_core.storage.postgres import get_backend

from rheo_app_core import api_routes, auth_routes
from rheo_app_core.internal_app import internal_app as internal_app
from rheo_app_core.mcp_mount import McpSurfaceRouter, mounted_mcp
from rheo_app_core.startup import run_startup


def _dispose_backend() -> None:
    # Shutdown disposes the backend's engines (pooled connections) and leaves the
    # process-wide backend bound: its engines are recreated lazily on the next use,
    # so a lifespan that runs again in the same process (tests) finds the same
    # backend the session already holds.
    get_backend().dispose()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup, then the mounted ``mcp`` surface; unwound in reverse at shutdown.

    The surface is built after ``run_startup`` because it reads the routing settings
    startup has just checked, and because a tool call needs the registries startup
    fills. It is torn down before the engines are disposed, so no MCP session is
    still dispatching when its database connections go.
    """
    app.state.startup = await asyncio.to_thread(run_startup)
    try:
        async with mounted_mcp(app):
            yield
    finally:
        await asyncio.to_thread(_dispose_backend)


app = FastAPI(lifespan=lifespan)
app.add_middleware(McpSurfaceRouter)

public_app = app
"""``app`` bound to a second name. ``spec.md``'s architecture text and 11's
``serve.py`` both say ``public_app``; ``tests/test_healthz.py`` and
``tests/test_git_clean.py`` (neither in this run's scope) keep importing ``app``
unchanged. One ``FastAPI()`` instance, two valid names (``00-index.md`` § Coupling
seams) — pick ``public_app`` in any new code, ``app`` only where an existing
0a-owned test file already imports it."""

public_app.include_router(auth_routes.router)
public_app.include_router(api_routes.router)
public_app.add_exception_handler(
    auth_routes.IdentityProviderUnavailable,
    auth_routes.identity_provider_unavailable_handler,
)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    """Liveness probe and the 02->03 seam.

    The exact body shape {"status": "ok", "contract_version": <int>} is what 03's
    web shell fetches and parses; contract_version is rheo_contracts.CONTRACT_VERSION,
    never a literal. tests/test_healthz.py pins it. No database call: liveness must
    answer while the startup sequence or a workspace migration is still running.
    """
    return {"status": "ok", "contract_version": CONTRACT_VERSION}
