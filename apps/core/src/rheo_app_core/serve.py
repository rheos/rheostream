"""The image entry point (C10): serves ``public_app`` (8000) and ``internal_app``
(8100) under one ``asyncio`` event loop, one process. The ``mcp`` surface is part of
``public_app`` (``mcp_mount.py``), so it needs no listener of its own.

Twenty-line composition (``spec.md``: "``serve()`` is a twenty-line asyncio
composition"), never ``uvicorn.run(..., workers=N)`` — ``worker`` is a declared
cut symbol this run does not build, and process-forking would give each worker
its own copy of the process-wide settings resolver and storage backend, which is
exactly what lets both apps share one without either needing its own startup
sequence (``main.py``'s ``lifespan`` runs once, on ``public_app`` alone).

Both servers bind ``0.0.0.0`` so a compose-network peer can reach either by the
service's DNS name; only ``public_app``'s port is ever published to the host
(``deploy/compose.yaml``, ``EXPOSE 8000`` below) — 8100 is container-network
only by construction, not by omitting a publish an operator could add back.
"""

import asyncio
from typing import Final

import uvicorn
from rheo_core.log_config import attach_access_redaction

from rheo_app_core.internal_app import internal_app
from rheo_app_core.main import public_app

GRACEFUL_SHUTDOWN_SECONDS: Final = 10
"""How long each server waits for open connections at shutdown before closing them.

Without a bound, uvicorn waits for every open connection, so one client holding a
response open (a slow reader, a stream) keeps the process from stopping and the
lifespan's shutdown half from running at all. Ten seconds covers any ordinary
request and is well inside a container runtime's default stop grace period."""


def server_configs() -> tuple[uvicorn.Config, uvicorn.Config]:
    """The public (8000) and internal (8100) ``uvicorn.Config``, with the access-log
    query redaction attached.

    Both write to the one ``uvicorn.access`` logger, and each ``uvicorn.Config``
    runs ``dictConfig`` as it is built, so the filter goes on after both exist
    (issue #287, FR 17). It covers the internal app too: nothing there needs a
    query string in a log."""
    public = uvicorn.Config(
        public_app,
        host="0.0.0.0",
        port=8000,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    internal = uvicorn.Config(
        internal_app,
        host="0.0.0.0",
        port=8100,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    attach_access_redaction()
    return public, internal


async def serve() -> None:
    public, internal = server_configs()
    await asyncio.gather(
        uvicorn.Server(public).serve(), uvicorn.Server(internal).serve()
    )


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
