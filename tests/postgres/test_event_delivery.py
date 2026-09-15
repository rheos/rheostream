"""Criterion 11 end to end, and AC 4 through AC 11's behavioural half: an event
published inside a transaction is delivered to its consumer exactly once, against a
real Postgres.

Seam: ``rheo_core.work.loop.visit_workspace``'s delivery drain — the public entry point
a worker pass actually calls — driven through ``rheo_core.events.publish``. No test here
calls one of the drain's private helpers, and **every assertion is a row read back in a
fresh transaction**, never a return value: the defects this file exists to catch — a
handler that ran twice, effects that committed without their ledger row, a delivery
marked ``delivered`` on work that was rolled back — show up in the rows and not in the
call.

**Three tests write a delivery row directly, and each says so where it does it.** They
are :func:`test_a_redelivery_of_a_processed_event_never_runs_the_handler_again`,
:func:`test_a_delivery_whose_worker_kept_dying_ends_failed_at_its_budget_and_not_before`
and :func:`test_a_stolen_lease_on_a_failing_handler_writes_no_outcome`, and in each the
write is **setup**, standing in for a state the system reaches only after a lapsed
lease or a run of crashes — never an assertion, and never a shortcut around the
behaviour under test. The blanket form of this sentence was wrong the moment the second
such test was added, which is why it now names the three.

**The consumer is ``tests/harness/consumers.py``'s, and it records two things in two
different ways on purpose.** Its domain effect is written on the
``HandlerUnitOfWork`` the drain handed it, so it is rolled back with the delivery's
transaction; its invocation marker is written on a second connection that commits
immediately, so it is *not*. That asymmetry is what makes "the handler body started"
observable after a rollback, after a primary-key conflict, and after a process death —
three events that would each erase an in-transaction counter and hide the double
invocation this file is here to detect. The harness module's own docstring carries the
full reasoning.

**Time is chosen, never slept.** Every ``now`` is a parameter of ``publish`` or of the
visit's clock, so a retry becomes due because a later instant is passed and not because
the suite waited. ``visit_workspace`` touches no control-plane index row, so unlike
``test_worker_loop.py`` this file may push its clock hours ahead: the wall-clock anchor
that file's docstring insists on is a constraint of ``record_visit``, which is reached
only through ``run_one_pass``.

**Each test gets its own provisioned workspace**, so a ``failed`` delivery blocking the
head of its ``(consumer_id, subject_ref)`` queue — which is the ratified behaviour, not
a leak — can never reach another case.
"""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.consumers import (
    CONSUMER_ID,
    EVENT_TYPE,
    ensure_consumer_tables,
    handler_that_raises,
    invocation_count,
    record_delivery,
    recorded_events,
)
from harness.consumers import registry as consumer_registry
from harness.registry import enable_harness_module
from rheo_contracts import EventEnvelope, WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.events import (
    DELIVERED,
    FAILED,
    LEASED,
    PENDING,
    ConsumerRegistry,
    NewEvent,
    lease_delivery,
    publish,
)
from rheo_core.operations import WORK_FAILURES, dispatch, register_core_operations
from rheo_core.settings import resolve
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import (
    consumer_processed,
    event_delivery,
    outbox_event,
)
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import (
    RETRY_BUDGET_EXHAUSTED,
    VisitResult,
    visit_workspace,
)
from rheo_core.work.operations import FailureList
from sqlalchemy import Engine, Row, delete, func, select, update

pytestmark = pytest.mark.postgres

OWNER = "worker-under-test"
THIEF = "worker-that-stole-the-lease"
SUBJECT = "harness.note:one"
BODY = "delivered once"
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The child process of the kill test writes nothing of its own: it publishes and dies.
#: ``os._exit(0)`` on the line after the commit is the whole point — no ``atexit``, no
#: interpreter shutdown, no chance for anything to tidy up after the transaction.
_PUBLISH_THEN_DIE = """
import os
import sys
from datetime import UTC, datetime
from uuid import UUID

from harness.consumers import EVENT_TYPE, registry
from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.events import NewEvent, publish
from rheo_core.storage.routing import open_unit_of_work

workspace_id = UUID(sys.argv[1])
subject_ref = sys.argv[2]
ctx = context_for_operator(workspace_id)
assert isinstance(ctx, WorkspaceContext), ctx
with open_unit_of_work(ctx) as uow:
    publish(
        ctx,
        uow,
        NewEvent(
            type=EVENT_TYPE,
            schema_version=1,
            subject_ref=subject_ref,
            subject_revision=1,
            data={"body": "published by a process that then died"},
        ),
        now=datetime.now(UTC),
        consumers=registry(),
    )
    uow.commit()
os._exit(0)
"""

#: What the child must be told rather than left to resolve. The session's control
#: database is named in ``tests/conftest.py`` and exists only for the length of this
#: run, and ``RHEO_CLUSTER_DSN`` is what the secret scope actually reads (the suite
#: bridges ``RHEO_TEST_CLUSTER_DSN`` onto it at import); a child that resolved its own
#: would reach the developer's real control database or no cluster at all.
CHILD_ENVIRONMENT = (
    "RHEO_PROFILE",
    "RHEO_CLUSTER_DSN",
    "RHEO__storage__control_database",
    "RHEO_DATA_ROOT",
)


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture
def engine(cluster: ClusterSession, workspace: UUID) -> Engine:
    """This test's own workspace database, with the harness module enabled and the
    consumer's two tables created.

    The module row is what puts ``harness`` in ``ctx.enabled_modules``, which is what
    the fan-out tests the subscription's ``module_id`` against; without it ``publish``
    writes the outbox row and **zero** delivery rows, and every assertion below would
    fail for a reason that has nothing to do with delivery.
    """
    built = cluster.backend.pools.engine_for(
        cluster.registry_row(workspace).database_name
    )
    with built.begin() as conn:
        enable_harness_module(conn)
        ensure_consumer_tables(conn)
    return built


@pytest.fixture
def now() -> datetime:
    """One instant every test builds its timeline from."""
    return datetime.now(UTC)


@pytest.fixture
def ctx(workspace: UUID, engine: Engine) -> WorkspaceContext:
    """An operator context over this test's workspace, with ``harness`` enabled."""
    built = context_for_operator(workspace)
    assert isinstance(built, WorkspaceContext), built
    assert built.enabled_modules == frozenset({"harness"})
    return built


def _publish(
    ctx: WorkspaceContext,
    *,
    now: datetime,
    subject_ref: str = SUBJECT,
    body: str = BODY,
) -> UUID:
    """One published event, committed, through the real unit of work."""
    with open_unit_of_work(ctx) as uow:
        event_id = publish(
            ctx,
            uow,
            NewEvent(
                type=EVENT_TYPE,
                schema_version=1,
                subject_ref=subject_ref,
                subject_revision=1,
                data={"body": body},
            ),
            now=now,
            consumers=consumer_registry(),
        )
        uow.commit()
    return event_id


def _visit(
    workspace_id: UUID,
    *,
    cluster: ClusterSession,
    consumers: ConsumerRegistry,
    at: datetime,
) -> VisitResult:
    """One whole workspace visit on a clock pinned to ``at``.

    The job registry is empty: this file publishes no job, so the drain's job half
    leases nothing and the delivery half is what runs.
    """
    return visit_workspace(
        DueWorkspace(workspace_id=workspace_id, observed_due_at=None),
        kinds=JobKindRegistry(),
        consumers=consumers,
        backend=cluster.backend,
        owner=OWNER,
        clock=lambda: at,
        jitter=None,
    )


def _delivery(engine: Engine, event_id: UUID) -> Row[Any]:
    with engine.connect() as conn:
        return conn.execute(
            select(event_delivery).where(
                event_delivery.c.event_id == event_id,
                event_delivery.c.consumer_id == CONSUMER_ID,
            )
        ).one()


def _ledger_rows(engine: Engine, event_id: UUID) -> int:
    """How many ``core.consumer_processed`` rows this consumer holds for the event."""
    with engine.connect() as conn:
        return int(
            conn.execute(
                select(func.count())
                .select_from(consumer_processed)
                .where(
                    consumer_processed.c.consumer_id == CONSUMER_ID,
                    consumer_processed.c.event_id == event_id,
                )
            ).scalar_one()
        )


def _observed(engine: Engine, event_id: UUID) -> tuple[tuple[UUID, ...], int]:
    """What the consumer recorded, and how many times its body started."""
    with engine.connect() as conn:
        return (
            tuple(row.event_id for row in recorded_events(conn)),
            invocation_count(conn, event_id=event_id),
        )


# --- AC 6: one published event, delivered once ----------------------------------------


def test_a_published_event_is_delivered_to_its_consumer_exactly_once(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    now: datetime,
) -> None:
    """AC 6, the ordinary path: publish, commit, one worker pass, one delivery.

    Four reads, because "delivered exactly once" is four separate facts: the consumer
    recorded the identifier once, its body started once, the dedup ledger holds one
    row, and the delivery row itself ended ``delivered`` with its lease released.

    **The fifth read happens while the handler is still running**, and it is what makes
    this test say something about the *transaction* rather than only about the outcome.
    The handler opens a second connection — the technique ``test_worker_loop.py``'s
    ``_live_lease`` already uses on the job side — and reads the delivery's own
    committed state. It must still be ``leased``: the ``delivered`` write shares the
    handler's transaction, so none of it is committed until after the handler returns.
    A delivery marked ``delivered`` in a transaction of its own, before the consumer
    that is supposed to earn it has even run, is visible here and nowhere else in this
    file — every outcome assertion below holds for that shape too.
    """
    event_id = _publish(ctx, now=now)

    pending = _delivery(engine, event_id)
    assert pending.state == PENDING and pending.attempts == 0

    seen: list[str] = []

    def reads_its_own_delivery_row_then_records(
        uow: HandlerUnitOfWork, envelope: EventEnvelope
    ) -> None:
        with uow.connection.engine.connect() as side:
            seen.append(
                str(
                    side.execute(
                        select(event_delivery.c.state).where(
                            event_delivery.c.event_id == envelope.id,
                            event_delivery.c.consumer_id == envelope.consumer_id,
                        )
                    ).scalar_one()
                )
            )
        record_delivery(uow, envelope)

    _visit(
        workspace,
        cluster=cluster,
        consumers=consumer_registry(reads_its_own_delivery_row_then_records),
        at=now,
    )

    assert seen == [LEASED], (
        "the delivery was already committed as delivered while its consumer was still "
        "running; that write belongs in the handler's own transaction"
    )

    recorded, invocations = _observed(engine, event_id)
    assert recorded == (event_id,)
    assert invocations == 1
    assert _ledger_rows(engine, event_id) == 1

    row = _delivery(engine, event_id)
    assert row.state == DELIVERED
    assert row.attempts == 1
    assert row.completed_at == now
    assert row.lease_owner is None and row.lease_until is None
    assert row.last_error is None


# --- AC 7: a redelivery never runs the handler again ----------------------------------


def test_a_redelivery_of_a_processed_event_never_runs_the_handler_again(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    now: datetime,
) -> None:
    """AC 7: the ledger row is present, so the second attempt marks the delivery
    ``delivered`` without entering the consumer at all.

    **The redelivery is manufactured by putting the row back to ``pending``**, which is
    what the race this guards actually leaves behind: a worker whose lease lapsed
    mid-handler has its finish refused and its attempt requeued, while the worker that
    took over has already committed the ledger row. Reproducing that race with two live
    workers would be a timing test; the state it produces is exactly this one.

    **Two assertions, and they are not interchangeable.** The invocation count is the
    real one — it reads 2 if the ``already_processed`` check is removed, because the
    handler body starts again and marks itself out of band before the resulting
    primary-key conflict rolls that attempt back. The delivery's own ``state`` is the
    second, independent signal: that same rollback leaves the row requeued rather than
    ``delivered``, so a weakened count assertion still has something behind it.
    """
    event_id = _publish(ctx, now=now)
    _visit(workspace, cluster=cluster, consumers=consumer_registry(), at=now)
    assert _observed(engine, event_id) == ((event_id,), 1)

    later = now + timedelta(minutes=1)
    with engine.begin() as conn:
        conn.execute(
            update(event_delivery)
            .where(
                event_delivery.c.event_id == event_id,
                event_delivery.c.consumer_id == CONSUMER_ID,
            )
            .values(
                state=PENDING,
                next_attempt_at=later,
                completed_at=None,
                lease_owner=None,
                lease_until=None,
            )
        )

    _visit(workspace, cluster=cluster, consumers=consumer_registry(), at=later)

    recorded, invocations = _observed(engine, event_id)
    assert invocations == 1, (
        "the consumer's handler body started twice: a redelivery of an event already "
        "in core.consumer_processed must be marked delivered without running it"
    )
    assert recorded == (event_id,)
    assert _ledger_rows(engine, event_id) == 1
    assert _delivery(engine, event_id).state == DELIVERED


# --- AC 6: a raising handler leaves nothing behind ------------------------------------


def test_a_consumer_that_raises_leaves_no_ledger_row_no_delivered_state_no_effects(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    now: datetime,
) -> None:
    """AC 6's other half: the handler's effects, the ledger row and the ``delivered``
    write are one transaction, so a raise takes all three.

    **The invocation marker must survive**, and asserting that is the point of the
    fourth assertion rather than a flourish: it is the direct proof that the marker is
    genuinely out of band. A marker written on the delivery's own unit of work would
    vanish in this same rollback, and a test that only checked the domain effect's
    absence would pass on a counter that can never observe a double invocation.
    """
    event_id = _publish(ctx, now=now)

    _visit(
        workspace,
        cluster=cluster,
        consumers=consumer_registry(handler_that_raises("the consumer gave up")),
        at=now,
    )

    recorded, invocations = _observed(engine, event_id)
    assert recorded == (), "the handler's own effects survived its raise"
    assert _ledger_rows(engine, event_id) == 0
    row = _delivery(engine, event_id)
    assert row.state != DELIVERED
    assert row.state == PENDING, "one attempt of a budget of many is a retry"
    assert row.last_error is not None and "gave up" in row.last_error

    assert invocations == 1, (
        "the out-of-band invocation marker was rolled back with the delivery's "
        "transaction; it is written on its own connection precisely so it is not"
    )


# --- AC 8: a real process death between commit and delivery ---------------------------


def _child_environment() -> dict[str, str]:
    """The parent's own resolved values, named rather than inherited by accident."""
    env = dict(os.environ)
    for name in CHILD_ENVIRONMENT:
        value = os.environ.get(name)
        assert value, (
            f"{name} is not set in this process, so the child cannot be told it; "
            "tests/conftest.py sets all four before any rheo_core import"
        )
        env[name] = value
    # ``tests/`` is on ``sys.path`` only because pytest put it there, and the child is
    # not running under pytest.
    env["PYTHONPATH"] = str(_REPO_ROOT / "tests")
    return env


def test_a_process_that_dies_between_commit_and_delivery_delivers_exactly_once(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
) -> None:
    """AC 8: a child publishes, commits and is killed; the parent then delivers.

    A real process exit, not a simulated one — ``os._exit(0)`` on the line after the
    commit, so nothing between the transaction and the death can tidy up after it. The
    two processes share no registry object and no memory: the child builds its own
    consumer registry, and the only thing that crosses between them is the
    ``consumer_id`` string the ``core.event_delivery`` row itself carries.

    The parent's ``ctx`` fixture runs first, so the harness module is enabled and the
    consumer's tables exist before the child publishes into that workspace.
    """
    assert ctx.workspace_id == workspace

    child = subprocess.run(
        [sys.executable, "-c", _PUBLISH_THEN_DIE, str(workspace), SUBJECT],
        cwd=_REPO_ROOT,
        env=_child_environment(),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert child.returncode == 0, child.stderr

    with engine.connect() as conn:
        pending = conn.execute(select(event_delivery)).all()
    assert len(pending) == 1, "the child's publish left no delivery to recover"
    event_id = pending[0].event_id
    assert pending[0].state == PENDING

    # The child chose its own ``now`` from its own clock, which is later than this
    # test's ``now`` fixture; visiting on the row's own first-attempt instant is what
    # makes the delivery due, and needs no sleep to arrange.
    _visit(
        workspace,
        cluster=cluster,
        consumers=consumer_registry(),
        at=pending[0].next_attempt_at,
    )

    recorded, invocations = _observed(engine, event_id)
    assert recorded == (event_id,), "the event the dead process published was lost"
    assert invocations == 1, "the event the dead process published was delivered twice"
    assert _ledger_rows(engine, event_id) == 1
    assert _delivery(engine, event_id).state == DELIVERED


# --- AC 9 and AC 10: the budget, and the failure list ---------------------------------


def test_a_delivery_that_exhausts_its_budget_ends_failed_and_is_listed(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    now: datetime,
) -> None:
    """AC 9 and AC 10: ``work.max_attempts`` failing attempts, then ``failed``, and the
    row appears in ``core.work.failures``'s ``deliveries`` collection.

    The clock is driven from the row's own ``next_attempt_at`` rather than from a
    transcription of the backoff schedule: the assertion is that the budget is spent
    and the delivery ends terminal, not that this test can restate ``backoff.py``.
    One extra visit past the budget is attempted, so a delivery that quietly kept
    retrying would show up as a ninth attempt rather than as a passing test.
    """
    budget = resolve().get_int("work.max_attempts")
    event_id = _publish(ctx, now=now)
    consumers = consumer_registry(handler_that_raises("this consumer always fails"))

    at = now
    for _ in range(budget + 1):
        _visit(workspace, cluster=cluster, consumers=consumers, at=at)
        row = _delivery(engine, event_id)
        if row.state == FAILED:
            break
        assert row.state == PENDING, row.state
        at = row.next_attempt_at

    failed = _delivery(engine, event_id)
    assert failed.state == FAILED
    assert failed.attempts == budget
    assert failed.lease_owner is None and failed.lease_until is None
    assert failed.last_error is not None and "always fails" in failed.last_error
    assert _ledger_rows(engine, event_id) == 0

    outcome = dispatch(ctx, WORK_FAILURES, {})
    assert outcome.ok, outcome
    assert isinstance(outcome.result, FailureList)
    listed = outcome.result.deliveries
    assert [delivery.event_id for delivery in listed] == [event_id]
    assert listed[0].consumer_id == CONSUMER_ID
    assert listed[0].attempts == budget
    assert listed[0].last_error == failed.last_error
    assert listed[0].completed_at == failed.completed_at
    assert outcome.result.jobs == [], "no job failed here; the collections are separate"


# --- AC 9, crash arm: the budget the exception arm can never see ----------------------


def _crashed_worker_left_it(
    engine: Engine, event_id: UUID, *, attempts: int, lapsed_at: datetime
) -> None:
    """Put a delivery into the state a worker that died mid-handler leaves behind.

    A direct write, and setup rather than assertion: the state is ``leased`` by an
    owner that is never coming back, with ``lease_until`` already past and ``attempts``
    at whatever the run of crashes reached. Walking up to the budget by actually killing
    that many subprocesses would cost one process spawn per attempt to observe a
    boundary one row already describes exactly — and the boundary, not the arithmetic
    that reaches it, is what this test is about.
    """
    with engine.begin() as conn:
        conn.execute(
            update(event_delivery)
            .where(
                event_delivery.c.event_id == event_id,
                event_delivery.c.consumer_id == CONSUMER_ID,
            )
            .values(
                state=LEASED,
                lease_owner="worker-that-died",
                lease_until=lapsed_at,
                attempts=attempts,
            )
        )


def test_a_delivery_whose_worker_kept_dying_ends_failed_at_its_budget_and_not_before(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    now: datetime,
) -> None:
    """AC 9's crash arm, and **both** sides of the boundary it turns on.

    A crash raises nothing, so ``_write_delivery_failure``'s ``>=`` check is never
    reached on this path: a consumer that kills its process is re-leased for ever,
    never ``failed``, never in the failure list, and holding the head of its queue
    behind a row no operator can release. The pre-handler gate is the only thing that
    ends it.

    **Both halves are asserted because the gate's comparison is off-by-one from the
    exception arm's on purpose, and only one of the two halves can see the
    difference.** ``attempts`` is incremented by the lease, so:

    * ``granted`` was left at ``budget - 1``; re-leasing takes it to exactly ``budget``,
      which is the last attempt the budget owes it, and the handler **must** run. A gate
      written ``>=`` refuses this one and grants ``budget - 1`` runs where the job side
      grants ``budget``.
    * ``spent`` was left at ``budget``; re-leasing takes it to ``budget + 1``, one past
      the budget, and the handler must **not** run.

    A test that watched only the terminal state would pass on both forms: ``granted``
    ends ``delivered`` either way under ``>``, and under ``>=`` it ends ``failed`` —
    which is why the invocation count, not the state, is what separates them.
    """
    budget = resolve().get_int("work.max_attempts")
    lapsed_at = now - timedelta(seconds=1)

    granted = _publish(ctx, now=now, subject_ref="harness.note:granted")
    _crashed_worker_left_it(engine, granted, attempts=budget - 1, lapsed_at=lapsed_at)
    spent = _publish(ctx, now=now, subject_ref="harness.note:spent")
    _crashed_worker_left_it(engine, spent, attempts=budget, lapsed_at=lapsed_at)

    result = _visit(workspace, cluster=cluster, consumers=consumer_registry(), at=now)

    assert result.deliveries_acquired == 2, "both lapsed leases are re-acquirable"

    last_granted = _delivery(engine, granted)
    assert last_granted.state == DELIVERED, (
        "the attempt at exactly work.max_attempts is the last one the budget grants; "
        "a gate written >= instead of > refuses it and grants one run fewer than the "
        "job side does for the same configured budget"
    )
    assert last_granted.attempts == budget
    with engine.connect() as conn:
        assert invocation_count(conn, event_id=granted) == 1

    one_past = _delivery(engine, spent)
    assert one_past.state == FAILED
    assert one_past.attempts == budget + 1
    assert one_past.last_error == RETRY_BUDGET_EXHAUSTED
    assert one_past.lease_owner is None and one_past.lease_until is None
    with engine.connect() as conn:
        assert invocation_count(conn, event_id=spent) == 0, (
            "a delivery past its budget must be failed before the handler is entered"
        )
    assert _ledger_rows(engine, spent) == 0


# --- AC 6: a lease stolen mid-handler writes no outcome at all ------------------------


def _steal(engine: Engine, event_id: UUID, *, at: datetime) -> None:
    """Re-lease the row to another worker, from inside the handler that holds it."""
    with engine.begin() as conn:
        taken = lease_delivery(conn, owner=THIEF, now=at, lease_seconds=60)
    assert taken is not None and taken.event_id == event_id


def test_a_worker_whose_lease_is_stolen_mid_handler_discards_its_own_effects(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    now: datetime,
) -> None:
    """``mark_delivered`` returns ``False``, so nothing of the attempt is committed.

    The steal happens **inside** the handler, after it has already written, which is
    the only ordering that separates a commit-then-check implementation from a
    check-then-commit one — the same ordering ``test_worker_loop.py``'s job-side twin
    uses, and the same defect it exists to catch.

    This is also the case that closes the gap the mutation table left open. Marking the
    delivery ``delivered`` in a transaction *after* the handler's own has committed
    passes every other test in this file, because on a happy path the two shapes are
    indistinguishable. Here they are not: under that shape the loser's effects and its
    ledger row commit before ``mark_delivered`` is ever asked, and both assertions
    below go red.
    """
    event_id = _publish(ctx, now=now)
    stolen_at = now + timedelta(seconds=61)

    def writes_then_loses_its_lease(
        uow: HandlerUnitOfWork, envelope: EventEnvelope
    ) -> None:
        record_delivery(uow, envelope)
        _steal(engine, event_id, at=stolen_at)

    _visit(
        workspace,
        cluster=cluster,
        consumers=consumer_registry(writes_then_loses_its_lease),
        at=now,
    )

    recorded, invocations = _observed(engine, event_id)
    assert recorded == (), (
        "the loser's effects must be rolled back, not committed beside the winner's"
    )
    assert _ledger_rows(engine, event_id) == 0, (
        "a ledger row from the loser would make the winner's own attempt a no-op"
    )
    assert invocations == 1

    row = _delivery(engine, event_id)
    assert row.state == LEASED, "the loser must not overwrite the winner's state"
    assert row.lease_owner == THIEF
    assert row.completed_at is None


@pytest.mark.parametrize("at_the_budget", [False, True])
def test_a_stolen_lease_on_a_failing_handler_writes_no_outcome(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    now: datetime,
    at_the_budget: bool,
) -> None:
    """The other two ``False`` branches: ``requeue_delivery`` and ``fail_delivery``.

    Both are predicated on the same ``_held``, and which one the failure arm reaches is
    decided by the budget — so the two parameters are the two writes. ``fail_delivery``
    is the one that matters most: ``failed`` is the state nothing moves a row out of,
    so a lapsed owner landing it on the row its successor now holds would strand that
    whole ``(consumer_id, subject_ref)`` queue on an event that was in fact processed
    exactly once.

    The direct write is setup, as in the crash-arm test above: it places ``attempts``
    one short of the budget so the lease takes it to exactly the budget, which is where
    the failure arm chooses ``fail_delivery`` over ``requeue_delivery``.
    """
    budget = resolve().get_int("work.max_attempts")
    event_id = _publish(ctx, now=now)
    if at_the_budget:
        with engine.begin() as conn:
            conn.execute(
                update(event_delivery)
                .where(
                    event_delivery.c.event_id == event_id,
                    event_delivery.c.consumer_id == CONSUMER_ID,
                )
                .values(attempts=budget - 1)
            )
    stolen_at = now + timedelta(seconds=61)

    def raises_after_losing_its_lease(
        uow: HandlerUnitOfWork, envelope: EventEnvelope
    ) -> None:
        record_delivery(uow, envelope)
        _steal(engine, event_id, at=stolen_at)
        raise RuntimeError("and then it failed")

    _visit(
        workspace,
        cluster=cluster,
        consumers=consumer_registry(raises_after_losing_its_lease),
        at=now,
    )

    row = _delivery(engine, event_id)
    assert row.state == LEASED, (
        "a worker whose lease lapsed must not write the outcome of an attempt the "
        "row's new owner is now making"
    )
    assert row.lease_owner == THIEF
    assert row.last_error is None
    assert row.completed_at is None
    assert _ledger_rows(engine, event_id) == 0
    assert _observed(engine, event_id)[0] == ()


# --- the one raise that is terminal rather than retried -------------------------------


def test_a_delivery_whose_event_is_missing_is_terminal_rather_than_retried(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    now: datetime,
) -> None:
    """``event_delivery.event_id`` carries no foreign key, so this state is reachable.

    An outbox row that is not there now will not be there on the eighth attempt, so
    the delivery ends ``failed`` on its **first** attempt rather than spending a budget
    of retries and up to an hour of backoff each to reach the same place. That matches
    ``_fail_terminally``'s rule on the job side for an input that cannot parse, and it
    is why the raise carries its own exception type: a bare ``LookupError`` out of a
    consumer handler is genuinely retryable and must stay so.
    """
    event_id = _publish(ctx, now=now)
    with engine.begin() as conn:
        conn.execute(delete(outbox_event).where(outbox_event.c.id == event_id))

    _visit(workspace, cluster=cluster, consumers=consumer_registry(), at=now)

    row = _delivery(engine, event_id)
    assert row.state == FAILED, "a permanently unbuildable delivery is never requeued"
    assert row.attempts == 1, "terminal on the first attempt, not after the budget"
    assert row.last_error is not None and "has no outbox row" in row.last_error
    assert row.lease_owner is None and row.lease_until is None
    with engine.connect() as conn:
        assert invocation_count(conn, event_id=event_id) == 0
