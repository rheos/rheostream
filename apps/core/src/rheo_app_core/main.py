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
and the MCP facade seam are 0b2's; the MCP transport is 0c's. ``rheo_app_mcp`` is
still imported at module scope to record the composition-root import edge the
architecture draws — now as :func:`build_mcp_surface`, which binds this process's
``ConsumerRegistry`` to the façade rather than merely naming the package.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from rheo_app_mcp.transport import build_mcp_app
from rheo_contracts import CONTRACT_VERSION
from rheo_core.storage.postgres import get_backend
from starlette.applications import Starlette

from rheo_app_core import api_routes, auth_routes
from rheo_app_core.internal_app import internal_app as internal_app
from rheo_app_core.startup import CONSUMERS, run_startup


def build_mcp_surface() -> Starlette:
    """The MCP façade, wired to **this** process's one ``ConsumerRegistry``.

    Not mounted in this run, and not called from anywhere in it: mounting the
    façade into this process's request-serving path is a later step
    (``rheo_app_mcp.transport``'s own docstring says so). What this function is for
    is the wiring decision, which belongs at the composition root and nowhere else.
    ``build_mcp_app`` takes ``consumers`` as a keyword with no default, so a
    mounting that forgot it would not compile rather than quietly publishing into a
    registry nobody subscribed to; naming :data:`~rheo_app_core.startup.CONSUMERS`
    here is what makes the in-process MCP surface and the HTTP routes above reach
    the same object once it is mounted.

    A function rather than a module-level application: constructing the SDK's
    session manager has real setup behind it, and the composition-root import edge
    this replaces did no work at all. Calling it is still the mounting step's job.
    """
    return build_mcp_app(consumers=CONSUMERS)


def _dispose_backend() -> None:
    # Shutdown disposes the backend's engines (pooled connections) and leaves the
    # process-wide backend bound: its engines are recreated lazily on the next use,
    # so a lifespan that runs again in the same process (tests) finds the same
    # backend the session already holds.
    get_backend().dispose()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup sequence before the first request; dispose the engines at shutdown."""
    app.state.startup = await asyncio.to_thread(run_startup)
    try:
        yield
    finally:
        await asyncio.to_thread(_dispose_backend)


app = FastAPI(lifespan=lifespan)

public_app = app
"""``app`` bound to a second name. ``spec.md``'s architecture text and 11's
``serve.py`` both say ``public_app``; ``tests/test_healthz.py`` and
``tests/test_git_clean.py`` (neither in this run's scope) keep importing ``app``
unchanged. One ``FastAPI()`` instance, two valid names (``00-index.md`` § Coupling
seams) — pick ``public_app`` in any new code, ``app`` only where an existing
0a-owned test file already imports it."""

public_app.include_router(auth_routes.router)
public_app.include_router(api_routes.router)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    """Liveness probe and the 02->03 seam.

    The exact body shape {"status": "ok", "contract_version": <int>} is what 03's
    web shell fetches and parses; contract_version is rheo_contracts.CONTRACT_VERSION,
    never a literal. tests/test_healthz.py pins it. No database call: liveness must
    answer while the startup sequence or a workspace migration is still running.
    """
    return {"status": "ok", "contract_version": CONTRACT_VERSION}
