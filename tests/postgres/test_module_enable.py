"""AC 13: ``core.module.enable``'s four steps, each of them on its own.

Seam: the operation dispatched through the **real** registry, against the real 5433
cluster. Nothing here calls ``module_enable_handler`` directly — what the contract
promises is that an operator or a CLI caller gets ``module_state_invalid`` or
``dependency_not_enabled`` under its own ``error_code``, and a test that called the
function would see the exception either way and prove nothing about which layer the
caller hears from.

**Every step has its own case, and that is the requirement rather than the style.** AC
13 is four steps, and a single case that drove all four and asserted the end state
would pass against an implementation that skipped one of them: the settings write and
the schedule write both land rows, so one "the rows are there" assertion reds for
either and distinguishes neither; and an implementation that never checked the
dependency would still reach ``enabled`` in the case where the dependency happens to be
enabled. So the two refusals are asserted separately from each other and from their
controls, the two writes are asserted in separate cases naming their own tables, and
the state move is asserted where it is observable — through the *next* context's
``enabled_modules``, which is what step 4 exists to change.

**The module is seeded through the repository writers, not installed through
``core.module.install``.** The subject here is enable, and driving a real install first
would put every case at the mercy of a migration chain it is not about;
``tests/postgres/test_module_lifecycle.py`` is where the two operations meet, and it
uses Recallatron for exactly that reason. The one thing seeding cannot fake is the
loaded manifest, which enable reads from step 2 on, so every case that gets past step 1
loads its fixture through the shared recipe.

**``set_module_state``'s return value is pinned, because assuming the row is the
mistake it exists to prevent.** Step 1 established that a row is there, so ``False`` at
step 4 is unreachable through any seeding this file could do — and an implementation
that discarded the answer would pass every other case in this file. The one case that
pins it replaces the writer at the call site, the way
``test_module_install.py::test_a_cancellation_after_the_last_write_rolls_the_module_state_row_back``
replaces ``insert_module_state`` at its own.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import (
    CONFIG_DEFAULT_SCHEDULE,
    CONFIG_EXPLICIT_DEFAULT,
    CONFIG_EXPLICIT_KEY,
    CONFIG_ID,
    CONFIG_OFF_SCHEDULE,
    CONFIG_OTHER_KEY,
    DEPENDANT_ID,
    PROVIDER_ID,
    PROVIDER_VERSION,
    loaded_probe_modules,
)
from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.modules import operations as module_operations
from rheo_core.modules.operations import (
    ABSENT_STATE,
    DEPENDENCY_NOT_ENABLED,
    ENABLED_STATE,
    INSTALLED_STATE,
    MODULE_ENABLE,
    MODULE_STATE_INVALID,
)
from rheo_core.operations import (
    HANDLER_FAILED,
    dispatch,
    register_core_operations,
)
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.storage import work_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.repositories import (
    ModuleStateRow,
    insert_module_state,
    list_module_states,
    workspace_settings,
)
from sqlalchemy import Engine, select

pytestmark = pytest.mark.postgres

FIXTURE_VERSION = "0.0.0"


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def registrations() -> None:
    """The shipped core operations on the process-wide registry (idempotent).

    ``core.module.enable`` is registered inside ``register_core_operations`` rather
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
    """An operator context: ``core.module.enable`` is ``owner, operator``."""
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


# --- driving the operation ------------------------------------------------------------


def enable(ctx: WorkspaceContext, module_id: str) -> OperationOutcome:
    """One dispatch through the real registry. Never the handler function."""
    return dispatch(ctx, MODULE_ENABLE, {"module_id": module_id})


def seed(
    engine: Engine,
    database: str,
    module_id: str,
    *,
    state: str,
    package_version: str = FIXTURE_VERSION,
) -> None:
    """One ``core.module_state`` row in ``state``, written by the shipped writer."""
    now = datetime.now(UTC)
    with UnitOfWork(engine, database) as uow:
        insert_module_state(
            uow.connection,
            module_id=module_id,
            package_version=package_version,
            state=state,
            installed_at=now,
            enabled_at=now if state == ENABLED_STATE else None,
        )
        uow.commit()


# --- reading the workspace ------------------------------------------------------------


def module_states(engine: Engine, database: str) -> dict[str, ModuleStateRow]:
    with UnitOfWork(engine, database) as uow:
        return {row.module_id: row for row in list_module_states(uow.connection)}


def settings_rows(engine: Engine, database: str) -> dict[str, str]:
    with UnitOfWork(engine, database) as uow:
        return workspace_settings(uow.connection)


def schedules_of(engine: Engine, database: str, module_id: str) -> dict[str, tuple]:
    """Every ``core.schedule`` row of ``module_id``, by name.

    The whole row rather than the name alone: a schedule created disabled, or pointed
    at the wrong job kind, or due immediately, is a different mistake from one that was
    never created, and each of them would pass a check that only counted names.
    """
    with UnitOfWork(engine, database) as uow:
        rows = uow.connection.execute(
            select(
                work_tables.schedule.c.name,
                work_tables.schedule.c.job_kind,
                work_tables.schedule.c.cron,
                work_tables.schedule.c.enabled,
                work_tables.schedule.c.last_run_at,
                work_tables.schedule.c.next_run_at,
            ).where(work_tables.schedule.c.module_id == module_id)
        ).all()
    return {str(row.name): tuple(row)[1:] for row in rows}


# --- step 1: the state check ----------------------------------------------------------


@pytest.mark.parametrize("seeded", [None, ENABLED_STATE, "removed"])
def test_enable_refuses_a_module_that_is_not_installed_or_disabled(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
    seeded: str | None,
) -> None:
    """Step 1, naming the state this workspace is **actually** in.

    Three states rather than one, because "has no usable row" and "has no row" are
    different rules and only the first is right. ``None`` is the never-installed case
    the contract calls ``absent``; ``enabled`` is the repeat that must not be a silent
    no-op, the same call Decision D makes for a repeat install; ``removed`` is a row
    that exists and is still not a state enable accepts. An implementation that asked
    ``row is None`` would pass the first and let the other two through.

    The module is **loaded** throughout, so the refusal cannot be about availability:
    what is wrong with each of these workspaces is the state, and the message says so.
    """
    expected = ABSENT_STATE if seeded is None else seeded
    if seeded is not None:
        seed(engine, database, CONFIG_ID, state=seeded)

    with loaded_probe_modules(monkeypatch, CONFIG_ID):
        outcome = enable(operator, CONFIG_ID)

    assert outcome.state == MODULE_STATE_INVALID, outcome
    assert outcome.error is not None
    assert outcome.error.error_code == MODULE_STATE_INVALID
    assert CONFIG_ID in outcome.error.error_text
    assert expected in outcome.error.error_text

    # A refusal writes nothing: not the row it would have moved, and not the
    # ``explicit_per_workspace`` row step 3 would have written for this fixture.
    rows = module_states(engine, database)
    assert {name: row.state for name, row in rows.items()} == (
        {} if seeded is None else {CONFIG_ID: seeded}
    )
    assert CONFIG_EXPLICIT_KEY not in settings_rows(engine, database)


def test_a_disabled_module_enables_again(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """Step 1's other accepted state: re-enable is the same operation.

    ``module-contract.md`` § Re-enable's whole contract is "same checks as enable", and
    a step 1 written as ``state == "installed"`` would pass every refusal case above
    while making a disabled module unrecoverable.
    """
    seed(engine, database, CONFIG_ID, state="disabled")

    with loaded_probe_modules(monkeypatch, CONFIG_ID):
        outcome = enable(operator, CONFIG_ID)

    assert outcome.ok, outcome
    assert module_states(engine, database)[CONFIG_ID].state == ENABLED_STATE


# --- step 2: the dependency check -----------------------------------------------------


def test_enable_refuses_a_required_dependency_that_is_installed_but_not_enabled(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """Step 2, and the difference from install's own dependency check is the point.

    The provider is **installed** in this workspace, at a version inside the dependant's
    declared range — which is exactly what ``core.module.install`` requires and lets
    through (``_INSTALLED_AT_MINIMUM``). Enable requires more: the dependency's
    behaviour has to be live, because from the next request the dependant's code may
    call it. An implementation that reused install's check would find nothing wrong
    here.
    """
    seed(
        engine,
        database,
        PROVIDER_ID,
        state=INSTALLED_STATE,
        package_version=PROVIDER_VERSION,
    )
    seed(engine, database, DEPENDANT_ID, state=INSTALLED_STATE)

    with loaded_probe_modules(monkeypatch, PROVIDER_ID, DEPENDANT_ID):
        outcome = enable(operator, DEPENDANT_ID)

    assert outcome.state == DEPENDENCY_NOT_ENABLED, outcome
    assert outcome.error is not None
    assert outcome.error.error_code == DEPENDENCY_NOT_ENABLED
    assert PROVIDER_ID in outcome.error.error_text
    assert INSTALLED_STATE in outcome.error.error_text

    # Neither row moved: the dependant is still installed and the provider is untouched.
    states = {name: row.state for name, row in module_states(engine, database).items()}
    assert states == {PROVIDER_ID: INSTALLED_STATE, DEPENDANT_ID: INSTALLED_STATE}


def test_a_dependency_that_is_enabled_satisfies_step_two(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """The control for the refusal above: with the provider enabled, nothing refuses.

    Its own case rather than a second half of the one above, so that relaxing step 2
    from ``enabled`` to ``installed`` reds exactly one of the two and a reader can tell
    which rule broke.
    """
    seed(
        engine,
        database,
        PROVIDER_ID,
        state=ENABLED_STATE,
        package_version=PROVIDER_VERSION,
    )
    seed(engine, database, DEPENDANT_ID, state=INSTALLED_STATE)

    with loaded_probe_modules(monkeypatch, PROVIDER_ID, DEPENDANT_ID):
        outcome = enable(operator, DEPENDANT_ID)

    assert outcome.ok, outcome
    assert module_states(engine, database)[DEPENDANT_ID].state == ENABLED_STATE


# --- step 3: the declared rows --------------------------------------------------------


def test_enable_writes_a_row_for_every_explicit_per_workspace_key(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """Step 3's settings half, asserted on the settings table alone.

    The fixture declares one key marked ``explicit_per_workspace`` and one not, so this
    case fails both ways: a step that wrote nothing, and a step that wrote every
    declared key regardless of the mark. The stored value is the **encoded package
    default** — ``"30"``, not ``30`` — because ``core.workspace_setting.value`` is text
    and ``encode_text`` is what the shipped writers put in it.
    """
    seed(engine, database, CONFIG_ID, state=INSTALLED_STATE)
    assert CONFIG_EXPLICIT_KEY not in settings_rows(engine, database)

    with loaded_probe_modules(monkeypatch, CONFIG_ID):
        outcome = enable(operator, CONFIG_ID)

    assert outcome.ok, outcome
    rows = settings_rows(engine, database)
    assert rows[CONFIG_EXPLICIT_KEY] == str(CONFIG_EXPLICIT_DEFAULT)
    assert CONFIG_OTHER_KEY not in rows


def test_enable_creates_a_row_for_every_schedule_enabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """Step 3's schedule half, asserted on ``core.schedule`` alone.

    Separate from the settings case above on purpose: one assertion covering both
    writes would red for either and name neither, which is not a test of two steps.

    The fixture declares one schedule marked ``enabled_by_default`` and one not. The
    created row is read whole — a schedule created *disabled*, pointed at the wrong job
    kind, or due the instant it was created is a different mistake from one never
    created, and a check that counted names would pass against all three.
    """
    seed(engine, database, CONFIG_ID, state=INSTALLED_STATE)
    assert schedules_of(engine, database, CONFIG_ID) == {}
    before = datetime.now(UTC)

    with loaded_probe_modules(monkeypatch, CONFIG_ID):
        outcome = enable(operator, CONFIG_ID)

    assert outcome.ok, outcome
    created = schedules_of(engine, database, CONFIG_ID)
    assert set(created) == {CONFIG_DEFAULT_SCHEDULE}
    job_kind, cron, enabled, last_run_at, next_run_at = created[CONFIG_DEFAULT_SCHEDULE]
    assert job_kind == f"{CONFIG_ID}.sweep"
    assert cron == "0 4 * * *"
    assert enabled is True
    assert last_run_at is None
    assert next_run_at > before, (next_run_at, before)
    assert CONFIG_OFF_SCHEDULE not in created

    # And the core's own ``core.retention_sweep`` row, written by the core chain, is
    # still the only other schedule this workspace has: enable created one row, not a
    # second copy of everything the table already held.
    assert set(schedules_of(engine, database, "core")) == {"retention_sweep"}


# --- step 4: the state move, and what it makes true -----------------------------------


def test_the_next_context_carries_the_enabled_module(
    monkeypatch: pytest.MonkeyPatch,
    workspace: UUID,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """Step 4, read where it is observable: the **next** context's enabled set.

    ``boundary/factories.py`` derives ``enabled_modules`` from ``core.module_state``
    rows whose state is ``enabled``, on every context it builds, and needs no code
    change for this — only a real row to read. So the assertion is made on a context
    built *after* the dispatch, not on the one that made it: the context this operation
    ran under was built before the row moved and will never carry the module, which is
    precisely what "from the next request" means.

    This is the case that reds if step 4 writes any state other than ``enabled``.
    """
    assert CONFIG_ID not in operator.enabled_modules
    seed(engine, database, CONFIG_ID, state=INSTALLED_STATE)

    with loaded_probe_modules(monkeypatch, CONFIG_ID):
        outcome = enable(operator, CONFIG_ID)

    assert outcome.ok, outcome
    assert outcome.result is not None
    assert outcome.result.module_id == CONFIG_ID  # type: ignore[attr-defined]

    # The context that made the call is unchanged; the next one carries the module.
    assert CONFIG_ID not in operator.enabled_modules
    after = context_for_operator(workspace)
    assert isinstance(after, WorkspaceContext), after
    assert CONFIG_ID in after.enabled_modules

    row = module_states(engine, database)[CONFIG_ID]
    assert row.state == ENABLED_STATE
    assert row.enabled_at is not None


def test_enable_fails_when_the_row_it_was_about_to_move_is_gone(
    monkeypatch: pytest.MonkeyPatch,
    workspace: UUID,
    engine: Engine,
    database: str,
    operator: WorkspaceContext,
) -> None:
    """Step 4 reads ``set_module_state``'s answer rather than assuming it.

    ``False`` means the ``UPDATE`` matched no row, and it is the only way an enable can
    learn that the row step 1 saw has since been removed — an ``UPDATE`` that matched
    nothing is otherwise indistinguishable from one that worked. Nothing this file can
    seed produces that answer, because step 1 has already established the row is there,
    so the writer is replaced where ``operations.py`` looks it up: the same technique
    ``test_module_install.py`` uses on ``insert_module_state``, and the only way to
    reach a branch the rest of the suite cannot.

    What this kills is an implementation that calls the writer and discards its result.
    That one reports a successful enable for a workspace whose module row is gone, and
    passes every other case in this file.
    """
    seed(engine, database, CONFIG_ID, state=INSTALLED_STATE)
    called: list[str] = []

    def answer_no(conn: object, **fields: object) -> bool:
        called.append(str(fields["module_id"]))
        return False

    monkeypatch.setattr(module_operations, "set_module_state", answer_no)

    with loaded_probe_modules(monkeypatch, CONFIG_ID):
        outcome = enable(operator, CONFIG_ID)

    assert called == [CONFIG_ID], (
        "the handler never reached step 4, so this case proves nothing about what it "
        f"does with the answer: {called}"
    )
    assert not outcome.ok, outcome
    # ``failed`` rather than a refusal state, and ``handler_failed`` rather than a
    # vocabulary of its own: this is not the caller's mistake and the contract's two
    # enable refusals do not describe it.
    assert outcome.state == "failed", outcome
    assert outcome.error is not None
    assert outcome.error.error_code == HANDLER_FAILED, outcome

    # And the transaction rolled back with it: the row is still ``installed`` and the
    # rows step 3 wrote a moment earlier are gone with the rest.
    assert module_states(engine, database)[CONFIG_ID].state == INSTALLED_STATE
    assert CONFIG_EXPLICIT_KEY not in settings_rows(engine, database)
    assert schedules_of(engine, database, CONFIG_ID) == {}
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext), ctx
    assert CONFIG_ID not in ctx.enabled_modules
