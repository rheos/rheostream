"""Two ``OverrideSource`` implementations over the ``core.workspace_setting`` and
``core.member_setting`` rows: one that opens its own connection, one that borrows the
caller's.

:class:`PostgresOverrideSource` is C2's. The resolver hands it a workspace id (and an
account id for member rows); it routes exactly as the storage layer does — the registry
row is read on every call and the workspace must be ``active`` — and reads the rows
through the workspace repositories inside a ``UnitOfWork``, so the test-profile
``current_database()`` assertion covers the settings read too. The protocol is C2's
contract and is not changed here.

:class:`TransactionBoundOverrideSource` is 1a1's, and exists for the readers that
already hold a connection: a retention value rechecked under the workspace lifecycle
lock, a sweep's own read, an export's snapshot read. Each of those wants the value
**this transaction** sees, and the class above would answer from a second pooled
connection with its own snapshot — a different question, one checkout further from the
budget, and (under the lifecycle lock) a second connection contending for rows the
first one holds.

Deliberately not re-exported from ``rheo_core.settings``: that package must not
import the storage layer at module level (the storage layer imports it).
"""

from collections.abc import Mapping
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import Connection

from rheo_core.settings.resolver import OverrideSource
from rheo_core.storage.backend import DATABASE_MISMATCH, StorageRefusal, UnitOfWork
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.repositories import member_settings, workspace_settings
from rheo_core.storage.routing import active_workspace


class PostgresOverrideSource:
    """Override rows from the workspace's own database.

    Takes no backend: ``active_workspace()`` reads the registry through the
    process-wide ``get_backend()``, so the engine comes from the same place.
    """

    def _open(self, workspace_id: UUID) -> UnitOfWork:
        row = active_workspace(workspace_id)
        pools = get_backend().pools
        engine = pools.engine_for(row.database_name, pin=True)
        return UnitOfWork(engine, row.database_name, pool=pools)

    def workspace_overrides(self, workspace_id: UUID) -> Mapping[str, str]:
        with self._open(workspace_id) as uow:
            return MappingProxyType(workspace_settings(uow.connection))

    def member_overrides(
        self, workspace_id: UUID, account_id: UUID
    ) -> Mapping[str, str]:
        with self._open(workspace_id) as uow:
            return MappingProxyType(member_settings(uow.connection, account_id))


class TransactionBoundOverrideSource:
    """Override rows read on a connection the caller already holds.

    Constructed from an open :class:`~sqlalchemy.Connection` (or the
    :class:`~rheo_core.storage.backend.UnitOfWork` around one) **and the workspace that
    connection is routed to**, which it is bound to for its whole life: this class
    opens nothing, so it cannot route, and a source that answered for any workspace id
    it was handed would read one workspace's database and call the rows another's. A
    mismatch is :data:`~rheo_core.storage.backend.DATABASE_MISMATCH`, the state the
    unit of work already uses for "this connection is not on the database you asked
    about" — loudly, because reading the wrong workspace's settings is exactly the
    silent failure the test-profile ``current_database()`` probe exists to catch.

    It does **not** re-check that the workspace is ``active``. The caller is already
    inside a transaction on that workspace's database, opened through the routing that
    made that check; re-reading the registry row here would be a second control-plane
    round trip per resolve, for an answer the open transaction already depends on.
    """

    __slots__ = ("_connection", "_workspace_id")

    def __init__(
        self, connection: Connection | UnitOfWork, *, workspace_id: UUID
    ) -> None:
        self._connection = (
            connection.connection if isinstance(connection, UnitOfWork) else connection
        )
        self._workspace_id = workspace_id

    def _bound(self, workspace_id: UUID) -> Connection:
        if workspace_id != self._workspace_id:
            raise StorageRefusal(
                DATABASE_MISMATCH,
                f"this override source is bound to workspace {self._workspace_id} "
                f"and cannot read workspace {workspace_id}",
            )
        return self._connection

    def workspace_overrides(self, workspace_id: UUID) -> Mapping[str, str]:
        return MappingProxyType(workspace_settings(self._bound(workspace_id)))

    def member_overrides(
        self, workspace_id: UUID, account_id: UUID
    ) -> Mapping[str, str]:
        return MappingProxyType(member_settings(self._bound(workspace_id), account_id))


_PROTOCOL_CHECK: type[OverrideSource] = PostgresOverrideSource
_TRANSACTION_BOUND_PROTOCOL_CHECK: type[OverrideSource] = TransactionBoundOverrideSource
