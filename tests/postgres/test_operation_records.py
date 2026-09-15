"""Criterion 13 / AC 15-20: the durable operation record, end to end.

**This file replaces ``tests/test_absent_behaviour.py::test_no_operation_lifecycle
_exists``**, removed in the same commit that landed the behaviour it denied — the
third criterion probe to leave by that route, after the worker loop's and the delivery
drain's. That probe searched three roots for one operation name, so an operation
record built anywhere else would have left it green for ever; what defends criterion
13 now is the behaviour below, driven through the real dispatcher and the real worker.

Seams: ``dispatch()``'s ``long_running`` minting path (the id readable before the work
runs), ``core.operation``'s own terminal-check constraint, and the worker's
terminalisation of the record beside the job's terminal write. The two **pre-handler**
gate cases — a job cancelled or found exhausted before its handler is entered — live
in ``tests/postgres/test_worker_loop.py`` instead, beside the gates they exercise; the
four cases here reach the other four, so between them the two files cover every one
of the six job-terminal-write sites that can carry an operation id.

**Nothing here queries ``core.operation`` directly** (AC 19). Every read goes through
``core.operation.get`` or ``core.operation.list``, dispatched the way a caller would,
except the one case whose whole subject is the database constraint — that one calls
the repository straight, deliberately, and says so.

**AC 20's wire half is not here.** The envelope is the two listeners' own contract and
is asserted where their machinery already lives:
``tests/postgres/test_api_surface.py::test_a_long_running_dispatch_carries_its_minted
_id_in_the_envelope`` and its internal-surface twin in
``tests/postgres/test_internal_operations.py``. What this file asserts is the
dispatcher's own half — the outcome carries the id, and a non-``long_running``
operation mints nothing at all.

**The controlled clock is wall-clock-anchored** for the same reason
``test_worker_loop.py``'s is, and that file's docstring carries the full reason: two
substrate functions this run may not edit read ``datetime.now(UTC)`` directly, so a
chosen instant makes correct code look flaky from three files away.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.registry import (
    NOTE_SCHEDULE,
    NOTE_SCHEDULE_DECLARATION,
    NOTE_SCHEDULE_KIND,
    NoteScheduled,
    NoteSchedulePayload,
    enable_harness_module,
    register_harness,
    run_scheduled_note,
)
from pydantic import BaseModel
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import (
    OPERATION_GET,
    OPERATION_LIST,
    OPERATION_RESOLVE,
    OPERATION_STATE,
    OperationRegistry,
    dispatch,
    register_core_operations,
)
from rheo_core.operations.core_ops import (
    OPERATION_RESOLVE_DECLARATION,
    WORKSPACE_STATUS,
)
from rheo_core.operations.records import (
    AUDIENCE_NONE,
    HANDLER_RETURNED,
    mark_unresolved,
    mint,
)
from rheo_core.operations.records import (
    finish_succeeded as finish_operation_succeeded,
)
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import NOT_FOUND
from rheo_core.settings import TEST_HARNESS_ORIGIN
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.postgres import PostgresBackend
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import job as job_table
from rheo_core.work.cancellation import CancellationToken, JobCancelled
from rheo_core.work.jobs import request_cancellation
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import LEASE_SECONDS, visit_workspace
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres

OWNER = "worker-under-test"
NOTE = "the work the operation stands for"


# --- fixtures and helpers -------------------------------------------------------------


@pytest.fixture(autouse=True)
def registrations(harness_keys: None) -> None:
    """The shipped core operations and the suite's harness module, on the
    process-wide registries. Both are idempotent."""
    register_core_operations()
    register_harness()


@pytest.fixture
def now() -> datetime:
    """One instant every test builds its timeline from, anchored to the wall clock."""
    return datetime.now(UTC)


@pytest.fixture
def database(cluster: ClusterSession, workspace: UUID) -> str:
    return cluster.registry_row(workspace).database_name


@pytest.fixture
def engine(cluster: ClusterSession, database: str) -> Engine:
    return cluster.backend.pools.engine_for(database)


@pytest.fixture
def owner(
    workspace: UUID,
    owner_account_id: UUID,
    engine: Engine,
    database: str,
) -> WorkspaceContext:
    """An owner context over a workspace whose ``harness`` module is enabled.

    The module row is what puts ``harness`` in the context's ``enabled_modules``, and
    without it every ``harness.*`` dispatch refuses ``module_disabled`` before its
    handler — which would make each test below pass its own assertion for the wrong
    reason if it asserted only on the record's absence.
    """
    with UnitOfWork(engine, database) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _kinds(handler: Callable[..., None] = run_scheduled_note) -> JobKindRegistry:
    """A registry holding only the one long-running harness job kind."""
    kinds = JobKindRegistry()
    kinds.register(NOTE_SCHEDULE_KIND, NoteSchedulePayload, handler)
    return kinds


def _visit(
    workspace_id: UUID,
    *,
    backend: PostgresBackend,
    at: datetime | None = None,
    kinds: JobKindRegistry | None = None,
) -> datetime:
    """One visit of one workspace on a pinned clock; returns the instant it ran on.

    **``at`` defaults to one second ahead of the real clock, and that default is
    load-bearing.** The job these tests watch is enqueued by the *handler*, inside
    the dispatch, so its ``next_run_at`` is a ``datetime.now(UTC)`` reading taken
    after the test's own ``now`` fixture was captured. A visit pinned to that fixture
    is a visit in the job's past: ``acquire_lease`` matches nothing, the handler never
    runs, and every assertion below fails reading ``pending`` — for the timeline and
    not for the behaviour. Reading the clock here instead puts the visit after the
    enqueue by construction. Wall-clock-anchored either way, per the module docstring.

    ``consumers`` is a fresh empty registry: nothing here publishes, so the delivery
    drain leases nothing and a shared instance would be state two cases could pass to
    each other.
    """
    instant = datetime.now(UTC) + timedelta(seconds=1) if at is None else at
    visit_workspace(
        DueWorkspace(workspace_id=workspace_id, observed_due_at=None),
        kinds=_kinds() if kinds is None else kinds,
        consumers=ConsumerRegistry(),
        backend=backend,
        owner=OWNER,
        clock=lambda: instant,
        jitter=None,
    )
    return instant


def _scheduled(
    ctx: WorkspaceContext, *, body: str = NOTE, max_attempts: int | None = None
) -> tuple[UUID, UUID]:
    """Dispatch the one ``long_running`` operation; return ``(operation_id, job_id)``.

    Asserts the dispatcher's own half of AC 15 on the way through: the outcome is
    ``pending``, not ``succeeded``, it carries an id, and the id the *handler* was
    handed is the same one the outcome reports — two sources for one value is the
    defect this helper would otherwise hide from every test that uses it.
    """
    payload: dict[str, object] = {"body": body}
    if max_attempts is not None:
        payload["max_attempts"] = max_attempts
    outcome = dispatch(ctx, NOTE_SCHEDULE, payload)
    assert outcome.state == "pending", outcome
    assert outcome.operation_id is not None, outcome
    assert outcome.result is not None
    result: Any = outcome.result
    assert result.operation_id == outcome.operation_id, outcome
    return outcome.operation_id, result.job_id


def _schedule(
    ctx: WorkspaceContext, *, body: str = NOTE, max_attempts: int | None = None
) -> UUID:
    """:func:`_scheduled`'s operation id alone, for the cases with no job to name."""
    return _scheduled(ctx, body=body, max_attempts=max_attempts)[0]


def _read(ctx: WorkspaceContext, operation_id: UUID) -> BaseModel:
    """One record, through the supported read (AC 19) rather than a query."""
    outcome = dispatch(ctx, OPERATION_GET, {"operation_id": str(operation_id)})
    assert outcome.ok, outcome
    assert outcome.result is not None
    return outcome.result


def _state(ctx: WorkspaceContext, operation_id: UUID) -> str:
    return str(_read(ctx, operation_id).state)  # type: ignore[attr-defined]


# --- AC 15: the id comes back before the work runs ------------------------------------


def test_the_minted_id_is_readable_while_the_work_is_still_pending(
    owner: WorkspaceContext,
) -> None:
    """AC 15: the record exists, is readable through the supported read, and is
    ``pending`` at a moment when the work has not run.

    "Has not run" is real here and not merely asserted: no worker visits this
    workspace in this test, so the job the handler queued is still sitting in
    ``core.job`` with nothing to lease it. The mutation this kills is minting after
    the handler returns instead of before the work transaction opens — under that
    mutation the id would still come back, and only the fact that the record is
    readable from a *different* transaction while the work is outstanding tells the
    two apart.
    """
    operation_id = _schedule(owner)

    record = _read(owner, operation_id)
    assert record.operation_id == operation_id  # type: ignore[attr-defined]
    assert record.state == "pending"  # type: ignore[attr-defined]
    assert record.name == NOTE_SCHEDULE  # type: ignore[attr-defined]
    assert record.safety_class == "mutate"  # type: ignore[attr-defined]
    # Nothing the state machine has not reached yet.
    assert record.terminal_at is None  # type: ignore[attr-defined]
    assert record.terminal_check_kind is None  # type: ignore[attr-defined]
    assert record.error_code is None  # type: ignore[attr-defined]


def test_the_record_is_readable_from_outside_while_the_handler_is_still_running(
    owner: WorkspaceContext,
) -> None:
    """AC 15's actual content, and the only case here that can tell the two wrong
    minting points apart.

    The test above reads the record *after* ``dispatch()`` returned, which a
    dispatcher that minted inside the work transaction — or after the handler — would
    also satisfy, because by then it has committed either way. So this reads it from
    **inside** the handler, through a second ``dispatch`` and therefore a second
    connection, at a moment when the work transaction is provably still open and the
    work provably has not finished.

    Both wrong minting points fail it, each for its own reason: minted inside the
    work transaction, the row is invisible to another connection and the read refuses
    ``not_found``; minted after the handler returns, there is no id to read with.

    The handler is registered on a **local** ``OperationRegistry`` — the pattern
    ``tests/test_handler_uow.py`` establishes — so the process-wide registry keeps
    the real ``harness.note.schedule`` and nothing registered here outlives the test.
    """
    seen: list[str] = []

    def reads_its_own_record(
        ctx: WorkspaceContext, uow: UnitOfWork, model_input: BaseModel
    ) -> BaseModel:
        operation_id = uow.operation_id if isinstance(uow, HandlerUnitOfWork) else None
        assert operation_id is not None, "the handler was handed no operation id"
        read = dispatch(ctx, OPERATION_GET, {"operation_id": str(operation_id)})
        assert read.ok, read
        assert read.result is not None
        result: Any = read.result
        seen.append(str(result.state))
        return NoteScheduled(job_id=uuid7(), operation_id=operation_id)

    registry = OperationRegistry()
    registry.register(
        NOTE_SCHEDULE_DECLARATION, reads_its_own_record, origin=TEST_HARNESS_ORIGIN
    )

    outcome = dispatch(owner, NOTE_SCHEDULE, {"body": NOTE}, registry=registry)

    assert outcome.state == "pending", outcome
    assert seen == ["pending"], (
        "the record must be committed and readable from another connection before "
        "the work transaction that follows it is even opened"
    )


def test_an_unknown_operation_id_refuses_not_found(owner: WorkspaceContext) -> None:
    """AC 19: ``get`` on an unknown id refuses; it does not answer an empty record."""
    outcome = dispatch(owner, OPERATION_GET, {"operation_id": str(uuid7())})
    assert outcome.state == NOT_FOUND, outcome
    assert outcome.result is None


# --- AC 16 and AC 17: the worker terminalises, one to one -----------------------------


def test_a_succeeded_job_terminalises_its_record_with_a_terminal_check(
    cluster: ClusterSession, workspace: UUID, owner: WorkspaceContext, now: datetime
) -> None:
    """AC 16 and AC 17's second clause, through the whole real flow.

    **This is the test that proves the production call site supplies good values.**
    The constraint test below proves only that the *database* refuses a bad write,
    which says nothing about whether the code path that actually writes ``succeeded``
    ever supplies a kind and an instant. So this reads the record back after a real
    mint, a real job run and a real terminalisation, and asserts both fields.
    """
    operation_id = _schedule(owner)
    assert _state(owner, operation_id) == "pending"

    _visit(workspace, backend=cluster.backend)

    record = _read(owner, operation_id)
    assert record.state == "succeeded"  # type: ignore[attr-defined]
    assert record.terminal_check_kind == HANDLER_RETURNED  # type: ignore[attr-defined]
    assert record.terminal_check_at is not None  # type: ignore[attr-defined]
    assert record.terminal_at is not None  # type: ignore[attr-defined]


def test_a_failed_job_terminalises_its_record_failed_and_not_succeeded(
    cluster: ClusterSession, workspace: UUID, owner: WorkspaceContext, now: datetime
) -> None:
    """AC 16: ``failed -> failed``, with no other mapping.

    ``max_attempts = 1`` so the exception arm's own budget check fires on the first
    raise: the subject is the terminalisation mapping, and the budget arithmetic is
    pinned in ``tests/postgres/test_worker_loop.py`` rather than re-proved here.
    """

    def raises(
        uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
    ) -> None:
        raise RuntimeError("the work did not get done")

    operation_id = _schedule(owner, max_attempts=1)
    _visit(workspace, backend=cluster.backend, kinds=_kinds(raises))

    record = _read(owner, operation_id)
    assert record.state == "failed"  # type: ignore[attr-defined]
    assert record.error_text == "the work did not get done"  # type: ignore[attr-defined]
    # AC 17 transitively: a record that reached ``failed`` records no terminal check,
    # because the constraint scopes that requirement to ``succeeded`` alone.
    assert record.terminal_check_kind is None  # type: ignore[attr-defined]


def test_a_cancelled_job_terminalises_its_record_cancelled(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    owner: WorkspaceContext,
    now: datetime,
) -> None:
    """AC 16: ``cancelled -> cancelled``, through the checkpoint window.

    The cancellation lands while the handler holds the lease and the handler then
    checkpoints, so the job ends through ``_resolve_zero_rowcount`` — the sixth of the
    worker's six terminal-write sites, and the one neither this file's other cases nor
    ``test_worker_loop.py``'s two gate cases reach.
    """
    operation_id, job_id = _scheduled(owner)

    def cancels_itself(
        uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
    ) -> None:
        with engine.begin() as conn:
            assert request_cancellation(conn, job_id, now=now) == "leased"
        token.checkpoint()
        raise AssertionError("the checkpoint must raise JobCancelled")

    _visit(workspace, backend=cluster.backend, kinds=_kinds(cancels_itself))

    assert _state(owner, operation_id) == "cancelled"


def test_an_unknown_job_kind_terminalises_its_record_failed(
    cluster: ClusterSession, workspace: UUID, owner: WorkspaceContext
) -> None:
    """AC 16 at ``_fail_terminally``: terminal on first sight, record included.

    The third of the worker's six terminal-write sites, and the only one neither the
    post-handler cases above nor ``test_worker_loop.py``'s two gate cases reach. An
    empty ``JobKindRegistry`` makes ``kinds.lookup`` raise ``JobKindUnknown``, which
    is terminal without a retry — an unknown kind cannot succeed on a later attempt —
    so the record must move with the job rather than sit ``pending`` waiting for a
    retry that will never be scheduled.
    """
    operation_id = _schedule(owner)

    _visit(workspace, backend=cluster.backend, kinds=JobKindRegistry())

    record = _read(owner, operation_id)
    assert record.state == "failed"  # type: ignore[attr-defined]
    # The job's own ``last_error`` verbatim, which is what says *which* of the four
    # routes to ``failed`` this was.
    assert record.error_text == (  # type: ignore[attr-defined]
        f"no handler is registered for job kind {NOTE_SCHEDULE_KIND!r}"
    )
    assert record.error_code == "job_failed"  # type: ignore[attr-defined]


def test_a_retry_is_not_a_finish_and_leaves_the_record_pending(
    cluster: ClusterSession, workspace: UUID, owner: WorkspaceContext, now: datetime
) -> None:
    """The requeue branch writes **nothing** to the operation record.

    A job that raises while still under its retry budget is going to run again:
    ``requeue_for_retry`` puts the row back to ``queued``. Terminalising its operation
    record here would report an operation ``failed`` while its own job is queued for
    another attempt — against AC 16's "when it finishes" — and would be
    unrecoverable, because the record is terminal and the next attempt's success
    would have nowhere to land.
    """

    def raises(
        uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
    ) -> None:
        raise RuntimeError("this attempt failed; there are two more")

    operation_id = _schedule(owner, max_attempts=3)
    _visit(workspace, backend=cluster.backend, kinds=_kinds(raises))

    record = _read(owner, operation_id)
    assert record.state == "pending", (  # type: ignore[attr-defined]
        "a retry is not a finish: the record must be exactly where it was before the "
        "attempt, not failed"
    )
    assert record.terminal_at is None  # type: ignore[attr-defined]
    assert record.error_code is None  # type: ignore[attr-defined]


# --- AC 17: the database refuses a succeeded row with no terminal check ---------------


def test_the_database_refuses_succeeded_without_a_terminal_check(
    engine: Engine, database: str, owner: WorkspaceContext, now: datetime
) -> None:
    """AC 17's first clause, in isolation: the **constraint** is what refuses it.

    The repository is called straight, with nulls, on purpose — this is a test of the
    schema and not of any production call site, and the production call site's own
    behaviour is asserted by
    :func:`test_a_succeeded_job_terminalises_its_record_with_a_terminal_check` above.
    The refusal is caught rather than pre-checked in Python: a Python guard here would
    prove only that the guard exists, and the claim is about the database.
    """
    with UnitOfWork(engine, database) as uow:
        operation_id = mint(
            uow.connection,
            name="harness.note.schedule",
            safety_class="mutate",
            actor_kind="operator",
            actor_id=None,
            entry="cli",
            audience_kind=AUDIENCE_NONE,
            audience_id=None,
            now=now,
        )
        uow.commit()

    def succeed_with_no_terminal_check() -> None:
        with UnitOfWork(engine, database) as uow:
            finish_operation_succeeded(
                uow.connection,
                operation_id=operation_id,
                now=now,
                terminal_check_kind=None,  # type: ignore[arg-type]
                terminal_check_at=None,  # type: ignore[arg-type]
            )
            uow.commit()

    with pytest.raises(IntegrityError) as excinfo:
        succeed_with_no_terminal_check()

    assert "operation_terminal_check_required" in str(excinfo.value)
    # And the row is untouched: the refused write took its whole transaction with it.
    assert _state(owner, operation_id) == "pending"


# --- AC 18: unresolved, and the explicit act that clears it ---------------------------


def test_resolve_clears_an_unresolved_record_to_a_named_outcome_with_a_note(
    engine: Engine, database: str, owner: WorkspaceContext, now: datetime
) -> None:
    """AC 18: ``core.operation.resolve`` moves an ``unresolved`` record to a named
    outcome with a note, and refuses a record in any other state.

    ``mark_unresolved`` is driven directly because release one ships no production
    caller for it — there is no external effect here whose outcome can go unknown —
    and AC 18 is about what clears the state, not about what sets it.
    """
    operation_id = _schedule(owner)
    # Still pending, so resolving it now must refuse: the record is not unresolved.
    refused = dispatch(
        owner,
        OPERATION_RESOLVE,
        {"operation_id": str(operation_id), "outcome": "failed", "note": "too early"},
    )
    assert refused.state == OPERATION_STATE, refused
    assert refused.error is not None and "pending" in refused.error.error_text

    with UnitOfWork(engine, database) as uow:
        assert mark_unresolved(uow.connection, operation_id=operation_id, now=now)
        uow.commit()
    assert _state(owner, operation_id) == "unresolved"

    outcome = dispatch(
        owner,
        OPERATION_RESOLVE,
        {
            "operation_id": str(operation_id),
            "outcome": "succeeded",
            "note": "the provider confirmed the effect by hand",
            "terminal_check_kind": "provider_status",
        },
    )
    assert outcome.ok, outcome
    record = _read(owner, operation_id)
    assert record.state == "succeeded"  # type: ignore[attr-defined]
    assert record.error_text == (  # type: ignore[attr-defined]
        "the provider confirmed the effect by hand"
    )
    assert record.terminal_check_kind == "provider_status"  # type: ignore[attr-defined]
    assert record.terminal_check_at is not None  # type: ignore[attr-defined]

    # Resolving twice refuses: the record is no longer unresolved.
    again = dispatch(
        owner,
        OPERATION_RESOLVE,
        {"operation_id": str(operation_id), "outcome": "failed", "note": "again"},
    )
    assert again.state == OPERATION_STATE, again


def test_resolving_to_succeeded_without_a_terminal_check_is_input_invalid(
    owner: WorkspaceContext,
) -> None:
    """AC 17 on the resolve path: refused by the input model, not by the constraint.

    The constraint would refuse the write too, but as a driver error the dispatcher
    folds into ``handler_failed`` carrying an exception class name — which tells the
    caller nothing about the field they left out.
    """
    outcome = dispatch(
        owner,
        OPERATION_RESOLVE,
        {
            "operation_id": str(uuid7()),
            "outcome": "succeeded",
            "note": "no check named",
        },
    )
    assert outcome.state == "input_invalid", outcome
    assert outcome.error is not None
    assert "terminal_check_kind" in outcome.error.error_text


def test_the_resolve_declaration_carries_an_audit_spec(
    owner: WorkspaceContext,
) -> None:
    """AC 18's "is itself audited", as far as this chunk reaches.

    The audit **row** is the next chunk's; what this chunk owes is a declaration that
    the registration rule will accept. A ``MUTATE`` declaration carrying
    ``audit = None`` is what makes ``register_core_operations()`` raise on every
    call once that rule lands, so this is checked here rather than discovered at
    startup a chunk later.
    """
    assert OPERATION_RESOLVE_DECLARATION.audit is not None
    assert OPERATION_RESOLVE_DECLARATION.audit.subject_field is None
    assert OPERATION_RESOLVE_DECLARATION.safety_class.value == "mutate"
    assert {role.value for role in OPERATION_RESOLVE_DECLARATION.roles} == {
        "owner",
        "operator",
    }


# --- AC 19: the supported reads ------------------------------------------------------


def test_list_is_newest_first_and_its_limit_is_bounded_at_the_model(
    owner: WorkspaceContext,
) -> None:
    """AC 19: ``core.operation.list`` pages, newest first, and an out-of-range
    ``limit`` never reaches the ``SELECT``."""
    minted = [_schedule(owner, body=f"note {index}") for index in range(3)]

    everything = dispatch(owner, OPERATION_LIST, {"limit": 10})
    assert everything.ok, everything
    assert everything.result is not None
    ids = [row.operation_id for row in everything.result.operations]  # type: ignore[attr-defined]
    assert ids == list(reversed(minted)), "newest created_at first"

    page = dispatch(owner, OPERATION_LIST, {"limit": 2})
    assert page.ok, page
    assert page.result is not None
    assert (
        [row.operation_id for row in page.result.operations]
        == list(  # type: ignore[attr-defined]
            reversed(minted)
        )[:2]
    )

    for out_of_range in (0, -1, 501):
        refused = dispatch(owner, OPERATION_LIST, {"limit": out_of_range})
        assert refused.state == "input_invalid", (out_of_range, refused)


# --- AC 20, dispatcher half: a non-long-running operation mints nothing ---------------


def test_a_non_long_running_operation_mints_no_record_at_all(
    owner: WorkspaceContext,
) -> None:
    """The default side of D2, which is every operation release one ships.

    Both halves in one test on purpose: the outcome's ``operation_id`` is ``None``
    *and* no row appeared. Asserting only the first would also pass for a dispatcher
    that minted a record and forgot to return its id, which is a worse defect than
    the one the assertion names.
    """
    before = dispatch(owner, OPERATION_LIST, {"limit": 500})
    assert before.ok, before
    assert before.result is not None
    started_with = len(before.result.operations)  # type: ignore[attr-defined]

    outcome = dispatch(owner, WORKSPACE_STATUS, {})
    assert outcome.ok, outcome
    assert outcome.operation_id is None, outcome

    after = dispatch(owner, OPERATION_LIST, {"limit": 500})
    assert after.ok, after
    assert after.result is not None
    assert len(after.result.operations) == started_with  # type: ignore[attr-defined]


# --- D1a: the event correlation id is the originating operation's ---------------------


def test_the_job_row_carries_the_minted_id_to_the_worker(
    engine: Engine, owner: WorkspaceContext
) -> None:
    """The channel itself, asserted directly rather than inferred.

    Every other case here reads the *record* and infers from it that the id reached
    the worker. This reads ``core.job.operation_id`` — the column this run gives its
    first writer — so the two defects can be told apart: a record that never moved
    because the id never reached the job is not the same bug as one that never moved
    because the terminal write is wrong. ``core.job`` is not ``core.operation``, so
    AC 19's no-direct-query rule does not reach this read.
    """
    operation_id, job_id = _scheduled(owner)

    with engine.connect() as conn:
        carried = conn.execute(
            select(job_table.c.operation_id).where(job_table.c.id == job_id)
        ).scalar_one()

    assert carried == operation_id


def test_an_abandoned_attempt_is_not_a_finish(
    cluster: ClusterSession, workspace: UUID, owner: WorkspaceContext, now: datetime
) -> None:
    """A handler that stops without finishing leaves the record exactly where it was,
    and a later visit that does finish moves it once.

    ``JobCancelled`` out of a handler whose row was never actually cancelled is the
    abandonment path: ``_run_handler`` rolls transaction 2 back and
    ``_resolve_zero_rowcount`` finds no cancellation request to honour, so it writes
    nothing at all — neither to the job nor to the record. The lease then lapses and
    the next visit runs the job properly.
    """
    operation_id = _schedule(owner, max_attempts=4)

    def abandons(
        uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
    ) -> None:
        raise JobCancelled("abandoned mid-flight")

    first = _visit(workspace, backend=cluster.backend, kinds=_kinds(abandons))
    assert _state(owner, operation_id) == "pending", (
        "an abandoned attempt is not a finish"
    )

    # The abandoned attempt left a live lease behind, so the next visit has to be
    # after it lapses or it leases nothing and this proves the wrong thing.
    later = first + timedelta(seconds=LEASE_SECONDS + 1)
    _visit(workspace, backend=cluster.backend, at=later)
    assert _state(owner, operation_id) == "succeeded"
