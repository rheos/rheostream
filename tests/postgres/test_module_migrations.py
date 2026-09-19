"""AC 10: one module's migration chain, applied through the door that raises.

Seams: ``run_module_chain`` against the real cluster (its own version table inside its
own schema, the ``core`` chain untouched, one ``core.module_schema_version`` row, and a
second call that is a clean no-op), and ``migrate_workspace``'s routing rule — which is
read here both as the exception it raises and as the ``control.workspace`` row it must
not have written, because an implementation that let the module chain through to the
``unavailable`` branch would satisfy the first half alone.

Recallatron is loaded through the real entry point, not a fixture manifest: it is a
real distribution from Prompt 4 on, and the whole point of this file is that a real
module chain runs.
"""

from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from uuid import UUID

import pytest
from conftest import ClusterSession
from rheo_core.migrations.module_chain import run_module_chain
from rheo_core.migrations.orchestrator import (
    CONTROL_CHAIN,
    CORE_CHAIN,
    migrate_workspace,
    recorded_revisions,
    script_location,
    version_table_for,
)
from rheo_core.modules import load_modules, loaded_manifests, reset_surfaces
from rheo_core.operations.registry import OperationRegistry
from rheo_core.refs.resolver import ResolverRegistry
from rheo_core.storage.control_tables import (
    CONTROL_SCHEMA,
    CONTROL_VERSION_TABLE,
    WorkspaceState,
)
from rheo_core.storage.core_tables import CORE_SCHEMA, CORE_VERSION_TABLE
from rheo_core.storage.provisioning import core_version
from rheo_core.storage.repositories import list_module_schema_versions
from rheo_core.tokens.sets import ToolRegistry
from rheo_recallatron import MANIFEST
from sqlalchemy import Engine, text

pytestmark = pytest.mark.postgres

MODULE_ID = MANIFEST.module_id
MODULE_VERSION_TABLE = f"alembic_version_{MODULE_ID}"
MODULE_HEAD = "0001_schema"
CORE_HEAD = "0006_runtime"


@pytest.fixture
def recallatron_loaded() -> Iterator[None]:
    """Recallatron in ``loaded_manifests()`` for one test, then gone.

    Through the real ``rheo.modules`` entry point, with ``allow`` naming it directly so
    the deployment's ``modules.installed`` setting is not involved. The registries are
    local because ``_LOADED`` is the only table this file reads; the skeleton declares
    nothing to register into them either way.
    """
    reset_surfaces()
    load_modules(
        registry=OperationRegistry(),
        resolvers=ResolverRegistry(),
        tools=ToolRegistry(),
        allow=frozenset({MODULE_ID}),
    )
    assert MODULE_ID in loaded_manifests(), (
        "recallatron's rheo.modules entry point was not discovered; the "
        "distribution is not installed in this environment"
    )
    yield
    reset_surfaces()


def workspace_engine(cluster: ClusterSession, workspace_id: UUID) -> tuple[str, Engine]:
    row = cluster.registry_row(workspace_id)
    return row.database_name, cluster.backend.pools.engine_for(row.database_name)


def tables_in(engine: Engine, schema: str) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema"
            ),
            {"schema": schema},
        ).scalars()
        return {str(name) for name in rows}


def schema_versions(engine: Engine, module_id: str) -> list[str]:
    with engine.connect() as connection:
        return [
            row.schema_version
            for row in list_module_schema_versions(connection)
            if row.module_id == module_id
        ]


def install(cluster: ClusterSession, workspace_id: UUID) -> None:
    """One ``run_module_chain`` call, in its own committed transaction."""
    database_name, engine = workspace_engine(cluster, workspace_id)
    with engine.begin() as connection:
        run_module_chain(
            connection,
            MANIFEST,
            expected_database=database_name,
            core_version=core_version(),
        )


# --- the three call sites -------------------------------------------------------------


def test_every_version_table_call_site_resolves_a_module_chain(
    cluster: ClusterSession, workspace: UUID, recallatron_loaded: None
) -> None:
    """``version_table_for`` is reached from three places, and all three must widen.

    ``_check_chain`` (through ``script_location``), ``recorded_revisions``, and
    ``run_chain``. A bare ``_VERSION_TABLES[...]`` subscript left at any of them
    ``KeyError``s on a module chain name the moment ``_check_chain`` stops rejecting
    one, because ``_VERSION_TABLES`` never gains a module entry.
    """
    assert version_table_for(CONTROL_CHAIN) == (CONTROL_SCHEMA, CONTROL_VERSION_TABLE)
    assert version_table_for(CORE_CHAIN) == (CORE_SCHEMA, CORE_VERSION_TABLE)
    assert version_table_for(MODULE_ID) == (MODULE_ID, MODULE_VERSION_TABLE)
    with pytest.raises(ValueError, match="unknown migration chain"):
        version_table_for("nope_probe")

    # Call site 1, ``_check_chain``: the chain now resolves a script directory, and it
    # is the module's own package rather than anything under ``rheo_core.migrations``.
    located = script_location(MODULE_ID)
    assert located == Path(str(resources.files(MANIFEST.storage.migrations_path)))
    assert (located / "env.py").is_file()

    database_name, engine = workspace_engine(cluster, workspace)
    # Call site 2, ``recorded_revisions``: it reads the MODULE's table, which does not
    # exist yet on this workspace even though the core chain's does.
    with engine.connect() as connection:
        assert recorded_revisions(connection, MODULE_ID) == frozenset()
        assert recorded_revisions(connection, CORE_CHAIN) == {CORE_HEAD}

    install(cluster, workspace)

    # Call site 3, ``run_chain``: the schema it created under the advisory lock is the
    # module's own, and call site 2 now reads the module's head from it.
    assert MODULE_VERSION_TABLE in tables_in(engine, MODULE_ID)
    with engine.connect() as connection:
        assert recorded_revisions(connection, MODULE_ID) == {MODULE_HEAD}
    assert database_name == cluster.registry_row(workspace).database_name


def test_an_unloaded_module_id_is_not_a_chain_name(
    cluster: ClusterSession, workspace: UUID
) -> None:
    """Without ``recallatron_loaded`` nothing claims the name, so it is refused.

    The manifest lookup is the membership test: ``version_table_for`` never invents a
    ``(schema, table)`` pair for a name no loaded module answers to.
    """
    assert MODULE_ID not in loaded_manifests()
    with pytest.raises(ValueError, match="unknown migration chain"):
        version_table_for(MODULE_ID)


# --- AC 10 ----------------------------------------------------------------------------


def test_run_module_chain_installs_into_its_own_schema_and_records_one_step(
    cluster: ClusterSession, workspace: UUID, recallatron_loaded: None
) -> None:
    _, engine = workspace_engine(cluster, workspace)
    core_tables_before = tables_in(engine, CORE_SCHEMA)

    install(cluster, workspace)

    # The version table is inside the MODULE's schema. Named three ways because the
    # failure modes differ: the wrong schema, the public schema, and the core schema.
    assert MODULE_VERSION_TABLE in tables_in(engine, MODULE_ID)
    assert MODULE_VERSION_TABLE not in tables_in(engine, "public")
    assert MODULE_VERSION_TABLE not in tables_in(engine, CORE_SCHEMA)

    # The core chain is untouched: same head, same table set, and its own version
    # table still carries exactly the core revision.
    assert tables_in(engine, CORE_SCHEMA) == core_tables_before
    assert CORE_VERSION_TABLE in core_tables_before
    with engine.connect() as connection:
        assert recorded_revisions(connection, CORE_CHAIN) == {CORE_HEAD}

    assert schema_versions(engine, MODULE_ID) == [MODULE_HEAD]
    with engine.connect() as connection:
        rows = [
            row
            for row in list_module_schema_versions(connection)
            if row.module_id == MODULE_ID
        ]
    assert len(rows) == 1
    assert rows[0].core_version_at_apply == core_version()


def test_a_second_run_module_chain_is_a_clean_no_op(
    cluster: ClusterSession, workspace: UUID, recallatron_loaded: None
) -> None:
    """The revision snapshot yields nothing the second time, so nothing is inserted.

    This is what keeps Prompt 4's ``CREATE SCHEMA IF NOT EXISTS`` honest and pins
    "one row per *newly applied* revision": an implementation inserting a row per
    ``run_module_chain`` call passes the first case and reds here.
    """
    _, engine = workspace_engine(cluster, workspace)
    install(cluster, workspace)
    assert schema_versions(engine, MODULE_ID) == [MODULE_HEAD]

    install(cluster, workspace)  # no exception

    with engine.connect() as connection:
        assert recorded_revisions(connection, MODULE_ID) == {MODULE_HEAD}
    assert schema_versions(engine, MODULE_ID) == [MODULE_HEAD]


# --- FR 11's positive control ---------------------------------------------------------


def test_migrate_workspace_refuses_a_module_chain_and_writes_nothing(
    cluster: ClusterSession, workspace: UUID, recallatron_loaded: None
) -> None:
    """The startup door refuses a module chain, and the refusal costs the row nothing.

    **Why the module chain is rigged to fail first.** The row read below is only
    load-bearing if a ``migrate_workspace`` that let the chain through would actually
    write. So the module is installed, and then its version table is made to name a
    revision this code does not know: ``run_chain`` now raises ``schema_ahead`` for
    ``recallatron``. With the guard in place nothing reaches the loop. With the guard
    deleted, the loop's generic ``except`` catches that refusal and sets
    ``control.workspace.state = unavailable`` for the WHOLE workspace — core and every
    other module with it. That is the FR 11 violation, and the row is where it shows.
    """
    database_name, engine = workspace_engine(cluster, workspace)
    install(cluster, workspace)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE {MODULE_ID}.{MODULE_VERSION_TABLE} SET version_num = :version"
            ),
            {"version": "9999_from_the_future"},
        )

    before = cluster.registry_row(workspace)
    assert before.state is WorkspaceState.ACTIVE

    # ``ValueError``, not ``NotImplementedError``: this is a routing rule, the same
    # class the control chain already gets, not "not built yet". ``pytest.raises``
    # pins that — ``NotImplementedError`` is a ``RuntimeError`` and would not match.
    with pytest.raises(ValueError) as excinfo:
        migrate_workspace(cluster.backend, workspace, chains=(MODULE_ID,))
    message = str(excinfo.value)
    assert "run_module_chain" in message, message
    assert MODULE_ID in message, message
    assert "unavailable" in message, message

    # Untouched: the whole row, not just the state, so a rewritten ``state_detail`` or
    # a bumped ``state_changed_at`` fails here too. (``state_detail`` is not None on a
    # healthy workspace — provisioning leaves its last step's name there — so the
    # comparison is against the row as it was, never against a presumed empty one.)
    after = cluster.registry_row(workspace)
    assert after == before
    assert after.state is WorkspaceState.ACTIVE
    assert after.state_detail == before.state_detail

    # The guard is a PRE-loop routing rule: it fires before the control-plane read, so
    # even an id with no workspace row gets it rather than ``workspace_missing``.
    with pytest.raises(ValueError, match="run_module_chain"):
        migrate_workspace(cluster.backend, UUID(int=0), chains=(MODULE_ID,))

    # And the module's own door still surfaces that same failure to its caller.
    with engine.begin() as connection:
        with pytest.raises(Exception) as chain_failure:
            run_module_chain(
                connection,
                MANIFEST,
                expected_database=database_name,
                core_version=core_version(),
            )
    assert "9999_from_the_future" in str(chain_failure.value)
    assert cluster.registry_row(workspace) == before
