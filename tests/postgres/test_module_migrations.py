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

import ast
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import (
    chain_head,
    create_required_extensions,
    loaded_probe_modules,
)
from rheo_core.migrations.module_chain import newly_applied, run_module_chain
from rheo_core.migrations.orchestrator import (
    CONTROL_CHAIN,
    CORE_CHAIN,
    MODULE_VERSION_TABLE_PREFIX,
    migrate_workspace,
    recorded_revisions,
    script_location,
    version_table_for,
)
from rheo_core.modules import loaded_manifests
from rheo_core.storage.control_tables import (
    CONTROL_SCHEMA,
    CONTROL_VERSION_TABLE,
    WorkspaceState,
)
from rheo_core.storage.core_tables import CORE_SCHEMA, CORE_VERSION_TABLE
from rheo_core.storage.provisioning import core_version
from rheo_core.storage.repositories import list_module_schema_versions
from rheo_recallatron import MANIFEST
from sqlalchemy import Engine, text

pytestmark = pytest.mark.postgres

MODULE_ID = MANIFEST.module_id
MODULE_VERSION_TABLE = f"alembic_version_{MODULE_ID}"
CORE_HEAD = "0007_record_deletion"
# The `core` chain's revisions, oldest first. Spelled out because this file uses them
# as the chain whose length is fixed and known: the module chain grows a revision
# whenever a run adds one, and a literal list of its revisions here would be a second
# place to remember. `module_revisions()` below reads the module's own instead.
CORE_REVISIONS = (
    "0001_core_schema",
    "0002_durable_work",
    "0003_approvals",
    "0004_standing_grants",
    "0005_export_records",
    "0006_runtime",
    "0007_record_deletion",
)


@pytest.fixture
def recallatron_loaded(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Recallatron in ``loaded_manifests()`` for one test, then gone.

    Through the harness's one loading recipe, which resolves the real ``rheo.modules``
    entry point for a real distribution and rebinds the loader's settings registry for
    the duration. That second half matters from the revision that gave Recallatron an
    ``explicit_per_workspace`` settings key: the process-global registry publishes no
    unregister, so a bare ``load_modules`` here would leave the key declared and
    provisioning would write its row into every workspace provisioned later in the
    session.
    """
    with loaded_probe_modules(monkeypatch, MODULE_ID):
        assert MODULE_ID in loaded_manifests(), (
            "recallatron's rheo.modules entry point was not discovered; the "
            "distribution is not installed in this environment"
        )
        yield


def module_head() -> str:
    """This chain's head revision, read off its script directory rather than pinned."""
    return chain_head(MANIFEST)


def module_revisions() -> tuple[str, ...]:
    """Every revision a fresh install applies, oldest first."""
    return newly_applied(
        MODULE_ID, before=frozenset(), after=frozenset({module_head()})
    )


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


def _env_configure_kwargs() -> dict[str, str]:
    """The string keywords the module's ``env.py`` passes to ``context.configure``.

    Parsed, not imported: ``env.py`` raises unless the orchestrator has already put a
    live connection on the Alembic config, which is the no-Alembic-by-hand rule doing
    its job. Only the two version-table keywords are compared below, so the dict is
    narrowed to them rather than asserting on every constant the file happens to pass.
    """
    source = (
        Path(str(resources.files(MANIFEST.storage.migrations_path))) / "env.py"
    ).read_text()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "configure"
        ):
            return {
                keyword.arg: keyword.value.value
                for keyword in node.keywords
                if keyword.arg in {"version_table", "version_table_schema"}
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            }
    raise AssertionError("the module's env.py makes no context.configure(...) call")


def install(cluster: ClusterSession, workspace_id: UUID) -> None:
    """One ``run_module_chain`` call, in its own committed transaction.

    The declared extensions go in first, because this file drives the chain directly
    rather than through ``core.module.install``, whose step 6 is what normally
    installs them. Without that step the chain fails inside the migration on a column
    type the database does not have — a missing install step wearing a broken
    migration's clothes.
    """
    database_name, engine = workspace_engine(cluster, workspace_id)
    with engine.begin() as connection:
        create_required_extensions(connection, MANIFEST)
        run_module_chain(
            connection,
            MANIFEST,
            expected_database=database_name,
            core_version=core_version(),
        )


# --- what "newly applied" means -------------------------------------------------------


def test_newly_applied_walks_the_history_rather_than_differencing_the_heads() -> None:
    """The rule ``run_module_chain``'s row count rests on, pinned where it is visible.

    **The ``core`` chain is the pin, and it stays the pin as the module chain grows.**
    Alembic's version table records only the current head, so upgrading a chain from
    base records **one** id while every revision on the way ran: the difference reports
    one applied step and the walk reports all of them. ``core`` has seven and is free —
    no migration has to be invented to prove it — while the module chain's length is a
    moving target that every later run changes. The idempotency case below does not
    catch a set difference either: it reds on the composite primary key, which is the
    database doing the assertion's job.
    """
    # Base to head: six steps ran, oldest first. `after - before` gives one.
    assert (
        newly_applied(CORE_CHAIN, before=frozenset(), after=frozenset({CORE_HEAD}))
        == CORE_REVISIONS
    )
    # A partial upgrade: four steps, and again the difference would give one.
    assert (
        newly_applied(
            CORE_CHAIN,
            before=frozenset({"0002_durable_work"}),
            after=frozenset({CORE_HEAD}),
        )
        == CORE_REVISIONS[2:]
    )
    # Agreeing snapshots apply nothing — this is what makes a re-run write no row,
    # independently of the primary key that would also have refused it.
    assert (
        newly_applied(
            CORE_CHAIN, before=frozenset({CORE_HEAD}), after=frozenset({CORE_HEAD})
        )
        == ()
    )
    # Oldest first, not newest first: Alembic walks downwards and the rows must not.
    walked = newly_applied(CORE_CHAIN, before=frozenset(), after=frozenset({CORE_HEAD}))
    assert walked[0] == "0001_core_schema" and walked[-1] == CORE_HEAD


# --- the three call sites -------------------------------------------------------------


def test_every_version_table_call_site_resolves_a_module_chain(
    cluster: ClusterSession, workspace: UUID, recallatron_loaded: None
) -> None:
    """``version_table_for`` is reached from three places, and all three must widen.

    ``script_location``, ``recorded_revisions``, and ``run_chain``. A bare
    ``_VERSION_TABLES[...]`` subscript left at any of them ``KeyError``s on a module
    chain name the moment the membership test stops rejecting one, because
    ``_VERSION_TABLES`` never gains a module entry.
    """
    assert version_table_for(CONTROL_CHAIN) == (CONTROL_SCHEMA, CONTROL_VERSION_TABLE)
    assert version_table_for(CORE_CHAIN) == (CORE_SCHEMA, CORE_VERSION_TABLE)
    assert version_table_for(MODULE_ID) == (MODULE_ID, MODULE_VERSION_TABLE)
    assert MODULE_VERSION_TABLE == f"{MODULE_VERSION_TABLE_PREFIX}{MODULE_ID}"
    with pytest.raises(ValueError, match="unknown migration chain"):
        version_table_for("nope_probe")

    # The other half of that pair, closed end to end: what the orchestrator DERIVES
    # has to be what the module's env.py actually CONFIGURES. The module's own
    # contract test asserts this statically, from the manifest and the prefix; here
    # it is asserted against the live derivation, so the two cannot drift apart
    # without one of them reding.
    assert _env_configure_kwargs() == {
        "version_table": MODULE_VERSION_TABLE,
        "version_table_schema": MODULE_ID,
    }

    # Call site 1, ``script_location``: the chain now resolves a script directory, and
    # it is the module's own package, not anything under ``rheo_core.migrations``.
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
        assert recorded_revisions(connection, MODULE_ID) == {module_head()}
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


def test_run_module_chain_installs_into_its_own_schema_and_records_each_step(
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

    assert schema_versions(engine, MODULE_ID) == list(module_revisions())
    with engine.connect() as connection:
        rows = [
            row
            for row in list_module_schema_versions(connection)
            if row.module_id == MODULE_ID
        ]
    # One row per applied revision, and every one stamped with the ``rheo-core`` that
    # applied it — not one row per ``run_module_chain`` call.
    assert len(rows) == len(module_revisions())
    assert {row.core_version_at_apply for row in rows} == {core_version()}


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
    assert schema_versions(engine, MODULE_ID) == list(module_revisions())

    install(cluster, workspace)  # no exception

    with engine.connect() as connection:
        assert recorded_revisions(connection, MODULE_ID) == {module_head()}
    assert schema_versions(engine, MODULE_ID) == list(module_revisions())


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

    # Deliberately not ``pytest.raises`` yet. FR 11's promise is about the ROW, so the
    # row is read first and asserted first: an implementation that let the chain
    # through to the ``unavailable`` write fails on the next three lines, before the
    # type of ``outcome`` is ever considered. Ordering the exception check first would
    # make the row read unreachable on exactly the violation it exists to catch.
    outcome: object
    try:
        outcome = migrate_workspace(cluster.backend, workspace, chains=(MODULE_ID,))
    except ValueError as exc:
        outcome = exc

    # Untouched: the whole row, not just the state, so a rewritten ``state_detail`` or
    # a bumped ``state_changed_at`` fails here too. (``state_detail`` is not None on a
    # healthy workspace — provisioning leaves its last step's name there — so the
    # comparison is against the row as it was, never against a presumed empty one.)
    after = cluster.registry_row(workspace)
    assert after == before, f"migrate_workspace wrote the row: {after}"
    assert after.state is WorkspaceState.ACTIVE
    assert after.state_detail == before.state_detail

    # Then the refusal itself. ``ValueError``, not ``NotImplementedError``: this is a
    # routing rule, the same class the control chain already gets, not "not built
    # yet" — and ``NotImplementedError`` is a ``RuntimeError``, so it would not
    # satisfy this check either.
    assert isinstance(outcome, ValueError), f"no refusal was raised: {outcome!r}"
    message = str(outcome)
    assert "run_module_chain" in message, message
    assert MODULE_ID in message, message
    assert "unavailable" in message, message

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
