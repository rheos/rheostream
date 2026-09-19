"""AC 11, AC 12 and Edge Cases 1, 2, 3 and 5: ``core.module.install``, end to end.

Seam: the operation dispatched through the **real** registry and run through the
**real** worker job loop. No test here calls the pre-flight handler or the job handler
directly, because the whole subject is the caller-visible split between the two
layers — five pre-flight refusals that keep their own ``error_code``, and three in-job
failures that all arrive as ``error_code = "job_failed"`` with their name in
``error_text``. A test that called either function would still see the right
exception and would prove nothing about which of those two shapes a caller gets.

**Every assertion about an in-job failure reads ``error_text`` and never
``error_code``.** ``work/loop.py``'s :data:`~rheo_core.work.loop.JOB_FAILED` is one
code for every route to ``failed`` and is shared by every job kind in this system.
Widening it into a per-kind code is not the repair for a test that wanted otherwise.

**Edge Case 7 is deliberately not here, and was not missed.** A ``modules.installed``
entry naming a module id nothing on disk provides is not an install-time failure: the
loader simply never loads it, so it never reaches ``loaded_manifests()`` and
``core.module.install`` never sees it as a candidate. That is
``tests/test_module_loader.py::test_a_named_module_that_is_not_installed_is_simply_absent``'s
property, asserted where the loading happens, and it is why the first pre-flight
refusal below reads the loaded set rather than the allowlist.

**Named deviation: the ratified Edge Case 3 asks for "a test role explicitly denied
the privilege", and this cluster cannot produce one.** The test cluster's ``rheo``
role is a PostgreSQL superuser, and a superuser is refused no extension whose files
are present, so the privilege branch of ``CREATE EXTENSION`` is unreachable through
the install path here. Two real failures stand in for it, and between them they cover
what the edge case is for — a ``CREATE EXTENSION`` the database genuinely refuses,
distinguished from a name it has never heard of:

- a name no installation provides (``noext_probe``), and
- a name this server *does* provide, into a workspace where creating it is refused
  because a conflicting object of that name already exists (``ext_probe`` again).

The second is the one that matters: the extension exists, the manifest is fine, and
the database still says no.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.module_migrations import PROBE_TABLE
from harness.modules import (
    ABSENT_EXTENSION_ID,
    ABSENT_EXTENSION_NAME,
    DEPENDANT_ID,
    DEPENDANT_RANGE,
    EXTENSION_ID,
    EXTENSION_NAME,
    MISSING_TESTS_ID,
    MISSING_TESTS_MANIFEST,
    OPTIONAL_DEPENDANT_ID,
    PROVIDER_ID,
    PROVIDER_VERSION,
    loaded_probe_modules,
)
from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.events import ConsumerRegistry
from rheo_core.migrations.orchestrator import MODULE_VERSION_TABLE_PREFIX
from rheo_core.modules.operations import (
    CONTRACT_TESTS_MISSING,
    DEPENDENCY_MISSING,
    DEPENDENCY_VERSION,
    EXTENSION_FAILED,
    MIGRATION_FAILED,
    MODULE_ALREADY_INSTALLED,
    MODULE_INSTALL,
    MODULE_UNAVAILABLE,
    ModuleInstallPayload,
    resolved_contract_tests,
    run_module_install_job,
)
from rheo_core.operations import OPERATION_GET, dispatch, register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.storage.backend import SCHEMA_AHEAD, UnitOfWork
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.repositories import (
    ModuleStateRow,
    insert_module_state,
    list_module_schema_versions,
    list_module_states,
    set_module_state,
)
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import JOB_FAILED, visit_workspace
from rheo_recallatron import MANIFEST as RECALLATRON_MANIFEST
from sqlalchemy import Engine, text

pytestmark = pytest.mark.postgres

OWNER = "module-install-under-test"
RECALLATRON_ID = RECALLATRON_MANIFEST.module_id
UNKNOWN_REVISION = "9999_from_the_future"
PROBE_REVISION = "0001_probe"


def version_table(module_id: str) -> str:
    return f"{MODULE_VERSION_TABLE_PREFIX}{module_id}"


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def registrations() -> None:
    """The shipped core operations on the process-wide registry (idempotent).

    ``core.module.install`` is registered inside ``register_core_operations`` rather
    than in the module-level tuple, so this fixture is also what makes the operation
    reachable here at all.
    """
    register_core_operations()


@pytest.fixture
def database(cluster: ClusterSession, workspace: UUID) -> str:
    return cluster.registry_row(workspace).database_name


@pytest.fixture
def engine(cluster: ClusterSession, database: str) -> Engine:
    return cluster.backend.pools.engine_for(database)


@pytest.fixture
def operator(workspace: UUID) -> WorkspaceContext:
    """An operator context: ``core.module.install`` is ``owner, operator``.

    Operator rather than owner because it needs no membership row, and because the
    ``rheo`` command is the caller this operation is written for.
    """
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


# --- driving the operation ------------------------------------------------------------


def install(ctx: WorkspaceContext, module_id: str) -> OperationOutcome:
    """One dispatch through the real registry. Never the handler function."""
    return dispatch(ctx, MODULE_INSTALL, {"module_id": module_id})


def run_the_worker(cluster: ClusterSession, workspace_id: UUID) -> None:
    """One real worker visit, with a registry holding only the install job kind.

    A local :class:`JobKindRegistry` rather than ``apps/worker``'s module-level
    ``JOB_KINDS``: that instance is the composition root's and
    ``tests/test_worker_job_kinds.py`` asserts its exact contents, so writing into it
    from here would make that file's answer depend on collection order. What this
    registry holds is the same three arguments the composition root registers.

    The clock is read here, one second ahead of the wall clock, because the job was
    enqueued by the *handler* during the dispatch above: a visit pinned to an instant
    captured before that would be a visit in the job's past, ``acquire_lease`` would
    match nothing, and every assertion would fail for the timeline rather than for the
    behaviour.
    """
    kinds = JobKindRegistry()
    kinds.register(MODULE_INSTALL, ModuleInstallPayload, run_module_install_job)
    instant = datetime.now(UTC) + timedelta(seconds=1)
    visit_workspace(
        DueWorkspace(workspace_id=workspace_id, observed_due_at=None),
        kinds=kinds,
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        owner=OWNER,
        clock=lambda: instant,
        jitter=None,
    )


def record_of(ctx: WorkspaceContext, operation_id: UUID | None) -> Any:
    """The operation record, through the supported read rather than a query."""
    assert operation_id is not None
    outcome = dispatch(ctx, OPERATION_GET, {"operation_id": str(operation_id)})
    assert outcome.ok, outcome
    assert outcome.result is not None
    return outcome.result


# --- reading the workspace ------------------------------------------------------------


def module_states(engine: Engine, database: str) -> dict[str, ModuleStateRow]:
    with UnitOfWork(engine, database) as uow:
        return {row.module_id: row for row in list_module_states(uow.connection)}


def schema_versions(engine: Engine, database: str, module_id: str) -> list[str]:
    with UnitOfWork(engine, database) as uow:
        return [
            row.schema_version
            for row in list_module_schema_versions(uow.connection)
            if row.module_id == module_id
        ]


def tables_in(engine: Engine, schema: str) -> set[str]:
    with engine.connect() as connection:
        return {
            str(name)
            for name in connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema"
                ),
                {"schema": schema},
            ).scalars()
        }


def schema_exists(engine: Engine, schema: str) -> bool:
    with engine.connect() as connection:
        return (
            connection.execute(
                text("SELECT to_regnamespace(:schema)"), {"schema": schema}
            ).scalar_one()
            is not None
        )


def extensions_in(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return {
            str(name)
            for name in connection.execute(
                text("SELECT extname FROM pg_extension")
            ).scalars()
        }


# --- AC 11: the extension step --------------------------------------------------------


def test_a_declared_extension_is_created_and_the_module_installs(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """AC 11's positive half, read as four separate facts about the workspace.

    The extension is there afterwards, the module's own schema carries both the table
    its revision created and the version table its ``env.py`` configured, the
    ``core.module_state`` row records the manifest's package version, and one
    ``core.module_schema_version`` row names the revision that ran. The version-table
    assertion is the one that catches a fixture chain left on Alembic's defaults,
    which would migrate cleanly while recording nothing.
    """
    assert EXTENSION_NAME not in extensions_in(engine)

    with loaded_probe_modules(monkeypatch, EXTENSION_ID):
        outcome = install(operator, EXTENSION_ID)
        assert outcome.state == "pending", outcome
        run_the_worker(cluster, workspace)
        record = record_of(operator, outcome.operation_id)

    assert record.state == "succeeded", record
    assert EXTENSION_NAME in extensions_in(engine)
    assert tables_in(engine, EXTENSION_ID) == {
        PROBE_TABLE,
        version_table(EXTENSION_ID),
    }

    row = module_states(engine, database)[EXTENSION_ID]
    assert row.state == "installed"
    assert row.package_version == "0.0.0"
    assert row.enabled_at is None
    assert schema_versions(engine, database, EXTENSION_ID) == [PROBE_REVISION]


def test_an_extension_the_server_does_not_provide_fails_before_any_migration(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """AC 11's negative half: named, and nothing of the module in the database.

    The three "nothing ran" assertions are separate on purpose. The module's schema
    is created by ``run_chain`` under the advisory lock, its version table by Alembic,
    and its ``core.module_state`` row by the step after the chain — so an
    implementation that ran the extension step too late, or not at all, leaves a
    different one of the three behind.
    """
    with loaded_probe_modules(monkeypatch, ABSENT_EXTENSION_ID):
        outcome = install(operator, ABSENT_EXTENSION_ID)
        assert outcome.state == "pending", outcome
        run_the_worker(cluster, workspace)
        record = record_of(operator, outcome.operation_id)

    assert record.state == "failed", record
    # The code is the loop's one code; the name lives only in the text.
    assert record.error_code == JOB_FAILED
    assert record.error_text.startswith(f"{EXTENSION_FAILED}: ")
    assert ABSENT_EXTENSION_NAME in record.error_text

    assert not schema_exists(engine, ABSENT_EXTENSION_ID)
    assert module_states(engine, database) == {}
    assert schema_versions(engine, database, ABSENT_EXTENSION_ID) == []


def test_an_extension_this_database_refuses_to_create_fails_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """Edge Case 3, as this cluster can express it: available, and still refused.

    The extension is confirmed *available* on this server first, so the failure below
    cannot be the previous case wearing a different name — and then a conflicting type
    of the same name is created in the workspace, which is what makes ``CREATE
    EXTENSION IF NOT EXISTS`` raise rather than skip. ``IF NOT EXISTS`` only skips
    when the *extension* is already installed; a colliding object is a real error.

    See this module's docstring for why the ratified "a role denied the privilege" is
    not what is driven here.
    """
    with engine.connect() as connection:
        available = connection.execute(
            text("SELECT count(*) FROM pg_available_extensions WHERE name = :name"),
            {"name": EXTENSION_NAME},
        ).scalar_one()
    assert available, f"{EXTENSION_NAME} is not available on this server at all"

    with engine.begin() as connection:
        connection.execute(text(f"CREATE TYPE public.{EXTENSION_NAME} AS ENUM ('x')"))

    with loaded_probe_modules(monkeypatch, EXTENSION_ID):
        outcome = install(operator, EXTENSION_ID)
        assert outcome.state == "pending", outcome
        run_the_worker(cluster, workspace)
        record = record_of(operator, outcome.operation_id)

    assert record.state == "failed", record
    assert record.error_code == JOB_FAILED
    assert record.error_text.startswith(f"{EXTENSION_FAILED}: ")
    assert EXTENSION_NAME in record.error_text
    assert EXTENSION_NAME not in extensions_in(engine)
    assert not schema_exists(engine, EXTENSION_ID)
    assert module_states(engine, database) == {}


# --- AC 12: the forced migration failure ----------------------------------------------


def test_a_module_migration_failure_leaves_the_control_workspace_row_untouched(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """AC 12. **The row is the assertion; the outcome is the corroboration.**

    ``control.workspace`` is read before and after and compared whole, and that
    comparison is made *first*. An implementation that routed a module chain through
    the startup door would flip this workspace to ``unavailable`` — taking ``core``
    and every other module down with it — and would **still** fail the install
    operation, so a test that only checked "the install failed" would pass against
    exactly the implementation AC 12 exists to forbid.

    The failure is forced with no fixture module at all: Recallatron's version table
    is pre-seeded with a revision this code does not know, so ``run_chain``'s own
    ``schema_ahead`` check raises from inside ``run_module_chain``.

    The name is ``migration_failed`` and never ``extension_failed``: by the time the
    chain runs, the extension step has already succeeded (Recallatron declares none),
    so the second name would describe the wrong event.
    """
    with engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {RECALLATRON_ID}"))
        connection.execute(
            text(
                f"CREATE TABLE {RECALLATRON_ID}.{version_table(RECALLATRON_ID)} "
                "(version_num varchar(32) NOT NULL)"
            )
        )
        connection.execute(
            text(
                f"INSERT INTO {RECALLATRON_ID}.{version_table(RECALLATRON_ID)} "
                "(version_num) VALUES (:version)"
            ),
            {"version": UNKNOWN_REVISION},
        )

    before = cluster.registry_row(workspace)
    assert before.state is WorkspaceState.ACTIVE

    with loaded_probe_modules(monkeypatch, RECALLATRON_ID):
        outcome = install(operator, RECALLATRON_ID)
        assert outcome.state == "pending", outcome
        run_the_worker(cluster, workspace)
        record = record_of(operator, outcome.operation_id)

    after = cluster.registry_row(workspace)
    assert after == before, f"the failed install wrote control.workspace: {after}"
    assert after.state is WorkspaceState.ACTIVE
    assert after.state_detail == before.state_detail

    assert record.state == "failed", record
    assert record.error_code == JOB_FAILED
    assert record.error_text.startswith(f"{MIGRATION_FAILED}: ")
    assert not record.error_text.startswith(f"{EXTENSION_FAILED}: ")
    assert SCHEMA_AHEAD in record.error_text
    assert UNKNOWN_REVISION in record.error_text

    # And the row the step after the chain would have written is not there, which is
    # what makes a crash-and-retry clean rather than an install claiming a schema it
    # never applied.
    assert module_states(engine, database) == {}


# --- Edge Case 1: module_unavailable --------------------------------------------------


def test_install_refuses_a_module_this_deployment_has_not_loaded(
    engine: Engine, database: str, operator: WorkspaceContext
) -> None:
    """Edge Case 1, with its own ``error_code`` and its own terminal record.

    The refusal is synchronous, so it keeps its name — that is the whole reason the
    operation is split across two layers — and the record the dispatcher minted is
    terminalised with that same name rather than with ``job_failed``.
    """
    outcome = install(operator, "no_such_module")

    assert outcome.state == MODULE_UNAVAILABLE, outcome
    assert outcome.error is not None
    assert outcome.error.error_code == MODULE_UNAVAILABLE
    assert "no_such_module" in outcome.error.error_text

    record = record_of(operator, outcome.operation_id)
    assert record.state == "failed"
    assert record.error_code == MODULE_UNAVAILABLE
    assert module_states(engine, database) == {}


# --- Edge Case 2: module_already_installed --------------------------------------------


@pytest.mark.parametrize("state", ["installed", "enabled"])
def test_install_refuses_a_module_this_workspace_already_has(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
    state: str,
) -> None:
    """Edge Case 2, in **both** states, naming the one the row is actually in.

    Parametrised rather than asserted once, because "a row exists" and "a row exists
    in a state install recognises" are different rules and only the first is correct:
    an implementation that refused ``installed`` and quietly proceeded for ``enabled``
    would pass a single-state case and then write a second row.

    The row is seeded through the repository writers rather than through a successful
    install, so this case is about the check and not about the job.
    """
    seeded = datetime.now(UTC)
    with UnitOfWork(engine, database) as uow:
        insert_module_state(
            uow.connection,
            module_id=EXTENSION_ID,
            package_version="0.0.0",
            state="installed",
            installed_at=seeded,
        )
        if state == "enabled":
            assert set_module_state(
                uow.connection,
                module_id=EXTENSION_ID,
                state="enabled",
                enabled_at=seeded,
            )
        uow.commit()

    with loaded_probe_modules(monkeypatch, EXTENSION_ID):
        outcome = install(operator, EXTENSION_ID)

    assert outcome.state == MODULE_ALREADY_INSTALLED, outcome
    assert outcome.error is not None
    assert EXTENSION_ID in outcome.error.error_text
    assert state in outcome.error.error_text

    # Still exactly one row, in the state it was seeded in: a refusal writes nothing.
    rows = module_states(engine, database)
    assert set(rows) == {EXTENSION_ID}
    assert rows[EXTENSION_ID].state == state


def test_set_module_state_answers_whether_a_row_was_there(
    engine: Engine, database: str
) -> None:
    """The second writer this phase adds, at its own seam.

    ``core.module.enable`` is the consumer and is the next phase's; what it needs from
    this function is the ``False`` that tells a missing row from a successful update,
    because an ``UPDATE`` that matched nothing is otherwise silent.
    """
    now = datetime.now(UTC)
    with UnitOfWork(engine, database) as uow:
        assert not set_module_state(
            uow.connection, module_id=EXTENSION_ID, state="enabled", enabled_at=now
        )
        insert_module_state(
            uow.connection,
            module_id=EXTENSION_ID,
            package_version="0.0.0",
            state="installed",
            installed_at=now,
        )
        assert set_module_state(
            uow.connection, module_id=EXTENSION_ID, state="enabled", enabled_at=now
        )
        uow.commit()

    row = module_states(engine, database)[EXTENSION_ID]
    assert row.state == "enabled"
    assert row.enabled_at is not None
    assert row.installed_at is not None


# --- Edge Case 5: the dependency refusals ---------------------------------------------


@pytest.fixture
def dependency_pair(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Both halves of the pair loaded, and neither installed in any workspace.

    Both have to *load* for either dependency refusal to be reachable at all:
    ``load_modules`` refuses a set whose required dependency is missing from it, so a
    dependant loaded alone would never get as far as install. What install adds is the
    second question — whether the dependency is installed **in this workspace** — and
    that is what these cases vary.
    """
    with loaded_probe_modules(monkeypatch, PROVIDER_ID, DEPENDANT_ID):
        yield


def test_install_refuses_a_required_dependency_this_workspace_lacks(
    dependency_pair: None,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """Edge Case 5: loaded by the deployment is not installed in the workspace."""
    outcome = install(operator, DEPENDANT_ID)

    assert outcome.state == DEPENDENCY_MISSING, outcome
    assert outcome.error is not None
    assert PROVIDER_ID in outcome.error.error_text
    assert DEPENDANT_RANGE in outcome.error.error_text
    assert module_states(engine, database) == {}


def test_install_refuses_a_dependency_at_a_version_outside_the_range(
    dependency_pair: None,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """Edge Case 5's other half, and a different refusal name from the one above.

    The workspace's recorded version is what is read, not the loaded manifest's: the
    provider is loaded at a version inside the range — it has to be, or the pair could
    not load together — and the row says something else. An implementation that
    checked the manifest would find nothing wrong here.
    """
    with UnitOfWork(engine, database) as uow:
        insert_module_state(
            uow.connection,
            module_id=PROVIDER_ID,
            package_version="2.0.0",
            state="installed",
            installed_at=datetime.now(UTC),
        )
        uow.commit()

    outcome = install(operator, DEPENDANT_ID)

    assert outcome.state == DEPENDENCY_VERSION, outcome
    assert outcome.error is not None
    assert PROVIDER_ID in outcome.error.error_text
    assert DEPENDANT_RANGE in outcome.error.error_text
    assert "2.0.0" in outcome.error.error_text
    assert set(module_states(engine, database)) == {PROVIDER_ID}


def test_a_dependency_installed_in_range_gets_past_pre_flight(
    dependency_pair: None,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """The control for the two refusals above: with the row right, nothing refuses.

    Deliberately stops at ``pending``, with no worker visit: what is under test is the
    dependency check, and running the job would put this case's outcome at the mercy
    of a migration it is not about.
    """
    with UnitOfWork(engine, database) as uow:
        insert_module_state(
            uow.connection,
            module_id=PROVIDER_ID,
            package_version=PROVIDER_VERSION,
            state="installed",
            installed_at=datetime.now(UTC),
        )
        uow.commit()

    outcome = install(operator, DEPENDANT_ID)

    assert outcome.state == "pending", outcome
    assert outcome.operation_id is not None


def test_an_absent_optional_dependency_does_not_refuse(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """``optional = True`` means the dependency may be absent from this workspace.

    Loaded alone — the provider is neither loaded nor installed — and pre-flight
    passes anyway. Nothing records the absence, because there is no column for it; the
    dependency is skipped and the install proceeds, which is what the contract's
    "recorded as absent, not refused" amounts to on this schema.
    """
    with loaded_probe_modules(monkeypatch, OPTIONAL_DEPENDANT_ID):
        outcome = install(operator, OPTIONAL_DEPENDANT_ID)

    assert outcome.state == "pending", outcome
    assert module_states(engine, database) == {}


# --- contract_tests_missing -----------------------------------------------------------


def test_install_refuses_a_module_whose_declared_contract_tests_are_absent(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """The fifth pre-flight refusal, and it says *absent from the checkout*.

    The wording matters because the check is only meaningful in a checkout: a
    ``contract_tests`` value is repo-relative and a built wheel does not package the
    module's test suite, which is exactly why this step resolves against the checkout
    root and is guarded by ``profile == "test"``. A module installed from a wheel into
    a deployment with no checkout is the case that guard excludes, not a module this
    refusal should ever describe as broken.

    The declared path is confirmed absent through the same public resolver the
    pre-flight step uses, so the fixture and the check cannot drift apart into a test
    that passes because *some* path was missing.
    """
    assert not resolved_contract_tests(MISSING_TESTS_MANIFEST).exists()

    with loaded_probe_modules(monkeypatch, MISSING_TESTS_ID):
        outcome = install(operator, MISSING_TESTS_ID)

    assert outcome.state == CONTRACT_TESTS_MISSING, outcome
    assert outcome.error is not None
    assert MISSING_TESTS_MANIFEST.contract_tests in outcome.error.error_text
    assert MISSING_TESTS_ID in outcome.error.error_text
    assert module_states(engine, database) == {}
