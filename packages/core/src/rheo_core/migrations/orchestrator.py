"""The migration orchestrator, the only entry point for Alembic.

Per chain it builds an ``alembic.config.Config`` in memory, points ``script_location``
at the chain's package directory (resolved through ``importlib.resources`` so the
installed wheel works, not just the checkout), and hands the live connection to the
environment through ``config.attributes["connection"]`` (the no-Alembic-by-hand rule
this serves is stated once, in ``rheo_core.migrations``). Before upgrading it:

- asserts ``SELECT current_database()`` equals the intended database name, the
  defence against a wrong-pool bug migrating the wrong database;
- takes the transaction-scoped advisory lock ``ADVISORY_LOCK_KEY`` on that database's
  connection **before** any DDL, so two core replicas, or a replica and the operator
  command, cannot interleave (the lock is released when the migration transaction
  ends; a session-level lock on a pooled connection would survive the pool's reset
  ``ROLLBACK`` after a failed migration and hang the next migrator);
- refuses ``schema_ahead`` when the database's version table names a revision the
  running code's script directory does not know.

``migrate_workspace`` never raises for a chain failure: it sets the registry row
``unavailable`` with the error text (or ``schema_ahead``) and returns, so startup
continues to the next workspace and completes. The control chain is different: a
failure there raises, because without the control plane nothing is routable.

Module chains in manifest dependency order, and the ``core.module_schema_version``
row per applied module step, are phase 2: ``chains`` is the hook, and anything other
than ``core`` is refused.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import UUID

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from rheo_core.storage.backend import (
    DATABASE_MISMATCH,
    SCHEMA_AHEAD,
    WORKSPACE_MISSING,
    StorageRefusal,
)
from rheo_core.storage.control_plane import (
    get_workspace,
    list_workspaces,
    set_workspace_state,
)
from rheo_core.storage.control_tables import (
    CONTROL_SCHEMA,
    CONTROL_VERSION_TABLE,
    WorkspaceState,
)
from rheo_core.storage.core_tables import CORE_SCHEMA, CORE_VERSION_TABLE
from rheo_core.storage.postgres import advisory_lock

if TYPE_CHECKING:
    from rheo_core.storage.postgres import PostgresBackend

logger = logging.getLogger("rheo_core.migrations")

CONTROL_CHAIN: Final = "control"
CORE_CHAIN: Final = "core"
CHAINS: Final[tuple[str, ...]] = (CONTROL_CHAIN, CORE_CHAIN)

# ``b"RHEOMIGR"`` as a big-endian 64-bit integer: fits a signed bigint, and shows up
# in ``pg_locks`` as ``classid = 0x5248454F``, ``objid = 0x4D494752``.
ADVISORY_LOCK_KEY: Final = 0x5248454F4D494752

_VERSION_TABLES: Final[dict[str, tuple[str, str]]] = {
    CONTROL_CHAIN: (CONTROL_SCHEMA, CONTROL_VERSION_TABLE),
    CORE_CHAIN: (CORE_SCHEMA, CORE_VERSION_TABLE),
}
_DETAIL_LIMIT: Final = 2000


def _check_chain(chain: str) -> str:
    if chain not in _VERSION_TABLES:
        raise ValueError(f"unknown migration chain {chain!r}; expected one of {CHAINS}")
    return chain


def script_location(chain: str) -> Path:
    """The chain's script directory inside the installed ``rheo_core`` package."""
    location = Path(
        str(resources.files("rheo_core.migrations").joinpath(_check_chain(chain)))
    )
    if not (location / "env.py").is_file():
        raise RuntimeError(f"migration chain {chain!r} has no env.py at {location}")
    return location


def build_config(chain: str, connection: Connection | None = None) -> Config:
    """An in-memory Alembic config for ``chain``. No ini file, no ``sqlalchemy.url``.

    With ``connection`` the environment can run; without it the environment refuses.
    """
    config = Config()
    config.set_main_option("script_location", str(script_location(chain)))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def known_revisions(chain: str) -> frozenset[str]:
    """Every revision id the running code's script directory holds for ``chain``."""
    script = ScriptDirectory.from_config(build_config(chain))
    return frozenset(revision.revision for revision in script.walk_revisions())


def recorded_revisions(connection: Connection, chain: str) -> frozenset[str]:
    """The revision ids the database's version table records; empty when absent."""
    schema, table = _VERSION_TABLES[_check_chain(chain)]
    qualified = f"{schema}.{table}"
    present = connection.execute(
        text("SELECT to_regclass(:name)"), {"name": qualified}
    ).scalar_one()
    if present is None:
        return frozenset()
    rows = connection.execute(text(f"SELECT version_num FROM {qualified}")).scalars()
    return frozenset(str(row) for row in rows)


def current_database(connection: Connection) -> str:
    return str(connection.execute(text("SELECT current_database()")).scalar_one())


def assert_current_database(connection: Connection, expected: str) -> None:
    """``database_mismatch`` unless ``connection`` is to ``expected``."""
    actual = current_database(connection)
    if actual != expected:
        raise StorageRefusal(
            DATABASE_MISMATCH,
            f"migration intended for database {expected!r} but the connection is "
            f"to {actual!r}; nothing was upgraded",
        )


def run_chain(connection: Connection, chain: str, *, expected_database: str) -> None:
    """Upgrade ``chain`` to head on ``connection``, inside the caller's transaction.

    The caller opens the transaction (``engine.begin()`` or a ``UnitOfWork``) and
    commits it; a connection outside a transaction is refused so a migration can never
    be silently rolled back at close. Raises ``database_mismatch``, ``schema_ahead``,
    or whatever the revision raised, in which case the caller's rollback undoes the
    partial DDL.
    """
    schema, _ = _VERSION_TABLES[_check_chain(chain)]
    if not connection.in_transaction():
        raise ValueError(
            "run_chain needs a connection inside a transaction the caller commits"
        )
    assert_current_database(connection, expected_database)
    # The lock comes first. ``CREATE SCHEMA IF NOT EXISTS`` is not race-safe on
    # ``pg_namespace``, and serialising exactly that DDL is what the lock is for.
    advisory_lock(connection, ADVISORY_LOCK_KEY)
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    ahead = recorded_revisions(connection, chain) - known_revisions(chain)
    if ahead:
        raise StorageRefusal(
            SCHEMA_AHEAD,
            f"database {expected_database!r} records {chain} revision(s) "
            f"{sorted(ahead)} that this code does not know",
        )
    command.upgrade(build_config(chain, connection), "head")


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """What one chain run did. ``ok`` is false when the row was set ``unavailable``."""

    workspace_id: UUID | None
    database_name: str
    chain: str
    ok: bool
    detail: str | None


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        described = f"{type(exc.orig).__name__}: {exc.orig}"
    else:
        described = f"{type(exc).__name__}: {exc}"
    return described[:_DETAIL_LIMIT]


def migrate_control(backend: "PostgresBackend") -> MigrationResult:
    """Ensure the control database exists, then bring the control chain to head."""
    backend.ensure_control_database()
    with backend.control_engine.begin() as connection:
        run_chain(connection, CONTROL_CHAIN, expected_database=backend.control_database)
    return MigrationResult(None, backend.control_database, CONTROL_CHAIN, True, None)


def migrate_workspace(
    backend: "PostgresBackend",
    workspace_id: UUID,
    *,
    chains: Sequence[str] = (CORE_CHAIN,),
) -> MigrationResult:
    """Run ``chains`` on one workspace database; never raise for a chain failure.

    ``workspace_missing`` for an unknown id is the one exception. ``control`` is not
    a workspace chain (``ValueError``); any other name is a module chain, which is
    a ``NotImplementedError`` until phase 2 wires manifest dependency order.
    """
    for chain in chains:
        if chain == CORE_CHAIN:
            continue
        if chain == CONTROL_CHAIN:
            raise ValueError("the control chain does not run on a workspace database")
        raise NotImplementedError(
            f"module migration chain {chain!r} is phase 2; only 'core' runs per "
            "workspace"
        )
    with backend.control_engine.connect() as connection:
        row = get_workspace(connection, workspace_id)
    if row is None:
        raise StorageRefusal(WORKSPACE_MISSING, f"workspace {workspace_id} has no row")
    with backend.pools.acquire(row.database_name) as engine:
        detail: str
        try:
            with engine.begin() as connection:
                for chain in chains:
                    run_chain(connection, chain, expected_database=row.database_name)
        except StorageRefusal as refusal:
            detail = (
                SCHEMA_AHEAD if refusal.state == SCHEMA_AHEAD else _error_text(refusal)
            )
        except Exception as exc:
            detail = _error_text(exc)
        else:
            return MigrationResult(row.id, row.database_name, CORE_CHAIN, True, None)
        with backend.control_engine.begin() as connection:
            set_workspace_state(
                connection,
                workspace_id,
                state=WorkspaceState.UNAVAILABLE,
                state_detail=detail,
            )
        logger.error(
            "workspace_migration_failed",
            extra={
                "workspace_id": str(workspace_id),
                "database_name": row.database_name,
                "state_detail": detail,
            },
        )
        return MigrationResult(row.id, row.database_name, CORE_CHAIN, False, detail)


def migrate_active_workspaces(
    backend: "PostgresBackend",
) -> tuple[MigrationResult, ...]:
    """The ``core`` chain for every ``active`` workspace, serially; a failure marks
    that one workspace ``unavailable`` and the loop continues."""
    with backend.control_engine.connect() as connection:
        rows = list_workspaces(connection, state=WorkspaceState.ACTIVE)
    return tuple(migrate_workspace(backend, row.id) for row in rows)
