"""The storage layer (A1): the control database, the per-workspace databases, engine
pools, server-derived routing, the unit of work, and provisioning.

- ``backend.py`` — the ``StorageBackend`` protocol, ``UnitOfWork`` (one connection,
  one transaction, one workspace database; no outbox or audit attachment point), and
  the storage refusals.
- ``postgres.py`` — ``PostgresBackend`` and the process-wide ``get_backend()``.
- ``pools.py`` — one engine per ``database_name`` in an LRU; busy engines stay.
- ``routing.py`` — ``route(ctx)`` and ``open_unit_of_work(ctx)``.
- ``control_tables.py`` / ``core_tables.py`` — the SQLAlchemy Core tables the
  migration chains import.
- ``control_plane.py`` / ``repositories.py`` — the only places SQL lives.
- ``provisioning.py`` — the five-step state machine with its fault-injection seam.
- ``data_root.py`` — the data root (FR-10), from C2.

This ``__init__`` re-exports the leaf layer only. ``routing`` and ``provisioning``
sit above ``rheo_core.migrations.orchestrator``, which imports this package's
submodules, so re-exporting them here would close an import cycle; import them by
module path. Inside the package, import from submodules, never from here.
"""

from rheo_core.storage.backend import (
    ACCOUNT_MISSING,
    DATABASE_MISMATCH,
    IDENTITY_EXISTS,
    SCHEMA_AHEAD,
    SLUG_TAKEN,
    UNIT_OF_WORK_CLOSED,
    WORKSPACE_EXISTS,
    WORKSPACE_MISSING,
    WORKSPACE_STATE,
    WORKSPACE_UNAVAILABLE,
    StorageBackend,
    StorageRefusal,
    UnitOfWork,
)
from rheo_core.storage.pools import EnginePool
from rheo_core.storage.postgres import PostgresBackend, get_backend, reset_backend

__all__ = [
    "ACCOUNT_MISSING",
    "DATABASE_MISMATCH",
    "IDENTITY_EXISTS",
    "SCHEMA_AHEAD",
    "SLUG_TAKEN",
    "UNIT_OF_WORK_CLOSED",
    "WORKSPACE_EXISTS",
    "WORKSPACE_MISSING",
    "WORKSPACE_STATE",
    "WORKSPACE_UNAVAILABLE",
    "EnginePool",
    "PostgresBackend",
    "StorageBackend",
    "StorageRefusal",
    "UnitOfWork",
    "get_backend",
    "reset_backend",
]
