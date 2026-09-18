"""Server-derived routing (FR-2): the workspace database is a pure function of the
authenticated context.

``route(ctx)`` accepts exactly one parameter, reads ``ctx.workspace_id`` and nothing
else, loads the ``control.workspace`` row **on every call** (the pool caches engines,
never this decision), refuses ``workspace_unavailable`` unless the row is ``active``,
and returns the pool for the row's ``database_name``. A database name, connection
string, schema or workspace id carried by any input never reaches this function,
because it reads none of those channels.

``open_unit_of_work(ctx)`` composes that read with ``UnitOfWork``. The unit of work's
expected database name is the registry row's, not the engine's own URL: an engine
cached under the wrong key would agree with itself, and the point of the assertion is
to catch exactly that.
"""

from uuid import UUID

from rheo_contracts import WorkspaceContext
from sqlalchemy import Engine

from rheo_core.storage.backend import WORKSPACE_UNAVAILABLE, StorageRefusal, UnitOfWork
from rheo_core.storage.control_plane import WorkspaceRow, get_workspace
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.postgres import get_backend


def active_workspace(workspace_id: UUID) -> WorkspaceRow:
    """The registry row for ``workspace_id``, read now; ``workspace_unavailable``
    unless its state is ``active`` (a missing row is unavailable too)."""
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a UUID")
    with get_backend().control_engine.connect() as connection:
        row = get_workspace(connection, workspace_id)
    if row is None:
        raise StorageRefusal(
            WORKSPACE_UNAVAILABLE,
            f"workspace {workspace_id} has no registry row",
            workspace_state=None,
        )
    if row.state is not WorkspaceState.ACTIVE:
        raise StorageRefusal(
            WORKSPACE_UNAVAILABLE,
            f"workspace {workspace_id} is {row.state.value}",
            workspace_state=row.state.value,
        )
    return row


def route(ctx: WorkspaceContext) -> Engine:
    """The engine for the context's workspace. One parameter, by design."""
    if not isinstance(ctx, WorkspaceContext):
        raise TypeError("route() takes a WorkspaceContext")
    row = active_workspace(ctx.workspace_id)
    return get_backend().pools.engine_for(row.database_name)


def open_unit_of_work(ctx: WorkspaceContext) -> UnitOfWork:
    """A unit of work on the context's workspace database, expected name from the
    registry row read by this call."""
    if not isinstance(ctx, WorkspaceContext):
        raise TypeError("open_unit_of_work() takes a WorkspaceContext")
    row = active_workspace(ctx.workspace_id)
    pools = get_backend().pools
    engine = pools.engine_for(row.database_name, pin=True)
    return UnitOfWork(engine, row.database_name, pool=pools)
