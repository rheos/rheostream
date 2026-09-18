"""``PostgresOverrideSource``: C2's ``OverrideSource`` protocol over the
``core.workspace_setting`` and ``core.member_setting`` rows.

The resolver hands it a workspace id (and an account id for member rows); this class
routes exactly as the storage layer does — the registry row is read on every call and
the workspace must be ``active`` — and reads the rows through the workspace
repositories inside a ``UnitOfWork``, so the test-profile ``current_database()``
assertion covers the settings read too. The protocol is C2's contract and is not
changed here.

Deliberately not re-exported from ``rheo_core.settings``: that package must not
import the storage layer at module level (the storage layer imports it).
"""

from collections.abc import Mapping
from types import MappingProxyType
from uuid import UUID

from rheo_core.settings.resolver import OverrideSource
from rheo_core.storage.backend import UnitOfWork
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


_PROTOCOL_CHECK: type[OverrideSource] = PostgresOverrideSource
