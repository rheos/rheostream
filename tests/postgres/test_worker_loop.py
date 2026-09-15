"""AC 3, 4, 5, 6 (loop half), 8 (crash arm), 10, 11, 13, 14, 15, 16 and 18: the worker
loop against a real Postgres, and the defence of benchmark criterion 12's **presence**.

This file replaces ``tests/test_absent_behaviour.py::test_no_worker_loop_exists``, which
was removed in the same commit that landed the behaviour it denied. That probe read one
path for one token, so a worker built anywhere else would have left it green for ever;
what defends criterion 12 now is the behaviour below, driven end to end.

Seams: ``rheo_core.work.loop``'s three public functions — ``visit_workspace``,
``run_one_pass`` and ``worker_loop`` — plus ``rheo_app_worker.main``'s
``install_stop_signals``. Every assertion reads the job row (or the index row, or the
``harness.note`` rows) back in a fresh transaction rather than trusting a return value:
the defects this file exists to catch — a loser's effects that committed, a cancelled
job requeued for a further retry — show up in the row and not in the call.

**The controlled clock is wall-clock-anchored, and that is a constraint, not a
preference.** Every ``now`` here is ``datetime.now(UTC) + timedelta(...)`` — a *delta*
from the real clock, never an arbitrary instant. Two substrate functions this run may
not edit read the wall clock directly: ``record_visit`` computes its floor as
``datetime.now(UTC) + work.due_reconcile_seconds`` and writes
``min(next_due_at, floor)``, and ``mark_work_due`` stamps ``updated_at`` from it. A
``now`` in the past, or more than 900 s into the future, therefore makes the AC 14 and
AC 15 assertions below wrong **on correct code**: the symptom is a flaky test and the
cause is three files away, in ``storage/work_index.py``.

**Every test that runs a whole pass first quiets the other workspaces.** The control
database is session-scoped, so by the time this file runs it holds a registry row for
every workspace the session has provisioned, and ``workspaces_with_due_work`` returns
never-visited workspaces first, capped at ``pools.cache_size``. Without
``only_workspaces`` a pass would wander into other tests' databases and might not reach
this test's workspace at all. Tests that exercise one visit call ``visit_workspace``
directly and need none of that.
"""

import os
import signal
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.consumers import EVENT_TYPE, ensure_consumer_tables
from harness.consumers import registry as consumer_registry
from harness.records import ensure_note_table, list_notes, write_note
from harness.registry import enable_harness_module
from pydantic import BaseModel
from rheo_app_worker.main import install_stop_signals
from rheo_core.boundary import context_for_operator
from rheo_core.events import ConsumerRegistry, NewEvent, publish
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.control_tables import workspace as workspace_table
from rheo_core.storage.postgres import PostgresBackend
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_index_tables import workspace_work_due
from rheo_core.storage.work_tables import job
from rheo_core.work import loop as loop_module
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import (
    acquire_lease,
    enqueue,
    enqueue_job,
    request_cancellation,
)
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import (
    IDLE_INTERVAL_SECONDS,
    LEASE_SECONDS,
    MAX_JOBS_PER_VISIT,
    MAX_PASS_BACKOFF_SECONDS,
    RETRY_BUDGET_EXHAUSTED,
    VisitResult,
    run_one_pass,
    visit_workspace,
    worker_loop,
)
from sqlalchemy import Engine, Row, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

pytestmark = pytest.mark.postgres

KIND = "harness.note.write"
OWNER = "worker-under-test"
THIEF = "worker-that-stole-the-lease"
RECONCILE_SECONDS = 900
BACKOFF_FIRST_RETRY = timedelta(seconds=5)
LONG_HANDLER = timedelta(seconds=30)
"""How far the #66 tests below push the clock from inside a handler.

Longer than ``BACKOFF_FIRST_RETRY``, which is the whole point: a handler that fails in
less than its own backoff is requeued into the future even on the stale instant, so
anything shorter than five seconds here would pass on the defect."""


class NotePayload(BaseModel):
    """The one synthetic job kind's input model. Lives here, not in ``tests/harness``:
    the registry is injectable, so there is no global to reset and no reason for a
    shared harness module."""

    body: str


def _writes_its_note(
    uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
) -> None:
    """The durable side effect K5's "absence of any later effect" is asserted over."""
    write_note(uow.connection, body=payload.body)


def _registry(
    handler: Callable[..., None] = _writes_its_note, *, kind: str = KIND
) -> JobKindRegistry:
    kinds = JobKindRegistry()
    kinds.register(kind, NotePayload, handler)
    return kinds


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture
def now() -> datetime:
    """One instant every test builds its timeline from, anchored to the wall clock."""
    return datetime.now(UTC)


@pytest.fixture
def database(cluster: ClusterSession, workspace: UUID) -> str:
    return cluster.registry_row(workspace).database_name


@pytest.fixture
def engine(cluster: ClusterSession, database: str) -> Engine:
    """The workspace database this file's jobs and notes live in."""
    engine = cluster.backend.pools.engine_for(database)
    with engine.begin() as conn:
        ensure_note_table(conn)
    return engine


@pytest.fixture
def fresh_backend(cluster: ClusterSession) -> Iterator[PostgresBackend]:
    """A real backend with its **own** engine pool, for the discovery assertions.

    AC 14 asserts that a workspace due later is never opened, and the session's own
    ``cluster.backend.pools`` has already cached an engine for every provisioned
    workspace during provisioning and migration — against that pool ``cached()`` names
    the later-due workspace before the pass even starts, and the assertion would hold
    for a correct loop and a sweeping one alike.

    ``verify_database=True`` matches the session's resolved ``test`` profile:
    ``PostgresBackend.__init__`` publishes that flag to ``UnitOfWork`` as a class
    attribute, so constructing one with it off would quietly disarm the
    wrong-database assertion for the rest of the session.
    """
    backend = PostgresBackend(
        cluster.backend.pools.cluster_url,
        control_database=cluster.backend.control_database,
        template_database=cluster.backend.template_database,
        pool_cache_size=cluster.backend.pools.cache_size,
        pool_max_connections=cluster.backend.pool_max_connections,
        pool_idle_close_seconds=300.0,
        verify_database=True,
    )
    try:
        yield backend
    finally:
        backend.dispose()


@pytest.fixture
def only_workspaces(cluster: ClusterSession) -> Callable[..., None]:
    """Push every other active workspace's ``due_at`` out of reach of this pass.

    Writes the index directly rather than through ``record_visit``, whose
    compare-and-set needs an observed value this fixture has no reason to read. It
    touches only workspaces other tests have already finished with; this file sorts
    after ``test_work_index.py``, which is the one other module that reads this table.
    """

    def _only(*keep: UUID) -> None:
        kept = set(keep)
        far = datetime.now(UTC) + timedelta(hours=1)
        with cluster.backend.control_engine.begin() as control:
            others = [
                row.id
                for row in control.execute(
                    select(workspace_table.c.id).where(
                        workspace_table.c.state == WorkspaceState.ACTIVE.value
                    )
                )
                if row.id not in kept
            ]
            for workspace_id in others:
                _set_due_at(control, workspace_id, far)

    return _only


# --- helpers --------------------------------------------------------------------------


def _set_due_at(control: Any, workspace_id: UUID, at: datetime) -> None:
    statement = pg_insert(workspace_work_due).values(
        workspace_id=workspace_id, due_at=at, updated_at=datetime.now(UTC)
    )
    control.execute(
        statement.on_conflict_do_update(
            index_elements=[workspace_work_due.c.workspace_id],
            set_={"due_at": at, "updated_at": statement.excluded.updated_at},
        )
    )


def _due_at(cluster: ClusterSession, workspace_id: UUID) -> datetime | None:
    with cluster.backend.control_engine.connect() as control:
        return control.execute(
            select(workspace_work_due.c.due_at).where(
                workspace_work_due.c.workspace_id == workspace_id
            )
        ).scalar_one_or_none()


def _put(
    engine: Engine,
    *,
    now: datetime,
    kind: str = KIND,
    payload: dict[str, object] | None = None,
    max_attempts: int = 3,
) -> UUID:
    """One queued job, committed, without going through the control plane."""
    with engine.begin() as conn:
        return enqueue_job(
            conn,
            kind=kind,
            payload={"body": "done"} if payload is None else payload,
            now=now,
            max_attempts=max_attempts,
        )


def _read(engine: Engine, job_id: UUID) -> Row[Any]:
    with engine.connect() as conn:
        return conn.execute(select(job).where(job.c.id == job_id)).one()


def _notes(engine: Engine) -> tuple[str, ...]:
    with engine.connect() as conn:
        return tuple(note.body for note in list_notes(conn))


def _visit_on(
    workspace_id: UUID,
    *,
    kinds: JobKindRegistry,
    backend: PostgresBackend,
    clock: Callable[[], datetime],
    owner: str = OWNER,
    consumers: ConsumerRegistry | None = None,
) -> VisitResult:
    """One visit of one workspace, on a clock the caller controls.

    ``consumers`` defaults to a **fresh empty** registry rather than a shared one:
    every job test in this file publishes nothing, so the delivery drain leases
    nothing, and a module-level instance would be state two cases could pass to
    each other.
    """
    return visit_workspace(
        DueWorkspace(workspace_id=workspace_id, observed_due_at=None),
        kinds=kinds,
        consumers=ConsumerRegistry() if consumers is None else consumers,
        backend=backend,
        owner=owner,
        clock=clock,
        jitter=None,
    )


def _visit(
    workspace_id: UUID,
    *,
    kinds: JobKindRegistry,
    backend: PostgresBackend,
    at: datetime,
    owner: str = OWNER,
    consumers: ConsumerRegistry | None = None,
) -> VisitResult:
    """One visit of one workspace, on a clock pinned to ``at``."""
    return _visit_on(
        workspace_id,
        kinds=kinds,
        backend=backend,
        clock=lambda: at,
        owner=owner,
        consumers=consumers,
    )


class AdvancingClock:
    """A controlled clock a handler can push forward while it runs.

    ``_visit`` above pins a **constant** instant, and that is exactly why issue #66's
    within-job half was invisible to every test in this file: on a frozen clock "the
    instant the job was leased" and "the instant its handler ended" are the same value,
    so no assertion can tell a loop that confuses them from one that does not.

    Wall-clock-anchored like everything else here — ``base`` is a real
    ``datetime.now(UTC)`` reading and every instant returned is ``base + offset``, a
    delta from the real clock rather than a chosen moment. See this module's docstring
    for what ``record_visit`` does to a test that forgets that.
    """

    def __init__(self, base: datetime) -> None:
        self.base = base
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return self.base + self.offset

    def advance(self, delta: timedelta) -> datetime:
        """Push the clock forward, and return the instant it now reads."""
        self.offset += delta
        return self()


def _live_lease(engine: Engine, *, owner: str = OWNER) -> Row[Any]:
    """This worker's one live lease, read on a second connection. Handlers only.

    The acquire commits in its own transaction before the handler is entered, so the
    row is visible here for the whole of that handler's life; every terminal and requeue
    write then nulls ``lease_owner`` and ``lease_until``, so exactly one row matches
    while a handler runs and none matches once the visit is over. ``.one()`` rather than
    ``.first()``: a second matching row would mean two live leases and must fail rather
    than be silently picked between.
    """
    with engine.connect() as conn:
        return conn.execute(
            select(job.c.id, job.c.lease_until).where(
                job.c.lease_owner == owner, job.c.state == "leased"
            )
        ).one()


class RecordingStop(threading.Event):
    """A stop event that records every ``wait`` and never actually sleeps.

    The timeouts are the assertion: they are how the idle interval and the capped
    doubling are observed without the suite spending a minute inside one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return self.is_set()


# --- AC 3 and AC 5: the ordinary path, and the crash-recovery one ---------------------


def test_a_visit_leases_runs_and_finishes_one_due_job(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """The whole seam in one case: acquire, run the handler, commit both together."""
    job_id = _put(engine, now=now)

    result = _visit(workspace, kinds=_registry(), backend=cluster.backend, at=now)

    assert result.jobs_acquired == 1
    assert result.next_due_at is None, "nothing is left to do in this workspace"
    row = _read(engine, job_id)
    assert row.state == "succeeded"
    assert row.attempts == 1
    assert row.finished_at == now
    assert row.lease_owner is None and row.lease_until is None
    assert _notes(engine) == ("done",), "the handler's effects commit with the success"


def test_a_job_whose_lease_expired_is_reacquired_and_runs_to_completion(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 5, with a controlled clock and **no** call to any release path.

    The first acquire is made directly and never finished, standing in for a killed
    worker; the visit that follows is the next pass.
    """
    job_id = _put(engine, now=now, max_attempts=3)
    with engine.begin() as conn:
        assert (
            acquire_lease(
                conn, owner="worker-that-died", now=now, lease_seconds=LEASE_SECONDS
            )
            is not None
        )

    lapsed = now + timedelta(seconds=LEASE_SECONDS + 1)
    result = _visit(workspace, kinds=_registry(), backend=cluster.backend, at=lapsed)

    assert result.jobs_acquired == 1
    row = _read(engine, job_id)
    assert row.state == "succeeded"
    assert row.attempts == 2, "the re-acquire counted the dead worker's attempt"
    assert _notes(engine) == ("done",)


# --- AC 4, loop half: two visitors, one workspace -------------------------------------


def test_a_second_visitor_never_takes_the_job_the_first_is_still_running(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 4's loop half: two visitors drain one workspace without overlapping.

    The second visit runs from **inside** the first one's handler, so visitor A holds a
    live, committed lease on its own job for the whole of B's visit. Visiting twice in
    sequence would prove nothing here: A's job is already ``succeeded`` by then, so B
    would skip it whatever the lease predicate did. ``attempts`` on A's job is the
    assertion that B never took it — a second lease is the only thing that moves it.
    """
    mine = _put(engine, now=now, payload={"body": "a"})
    theirs = _put(engine, now=now, payload={"body": "b"})
    others: list[VisitResult] = []

    def visits_again_while_still_leased(
        uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
    ) -> None:
        write_note(uow.connection, body=payload.body)
        others.append(
            _visit(
                workspace,
                kinds=_registry(),
                backend=cluster.backend,
                at=now,
                owner=THIEF,
            )
        )

    result = _visit(
        workspace,
        kinds=_registry(visits_again_while_still_leased),
        backend=cluster.backend,
        at=now,
    )

    assert result.jobs_acquired == 1, "A's second acquire found B had drained the rest"
    assert [visit.jobs_acquired for visit in others] == [1]
    assert _read(engine, mine).attempts == 1, "B must not lease a live-leased job"
    assert _read(engine, mine).state == "succeeded"
    assert _read(engine, theirs).attempts == 1
    assert _read(engine, theirs).state == "succeeded"
    assert sorted(_notes(engine)) == ["a", "b"], "each job ran exactly once"


# --- AC 8's crash arm: the retry budget on the way back in ---------------------------


def test_a_job_that_dies_on_every_attempt_reaches_failed_rather_than_relooping(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 8, crash arm, at ``max_attempts = 1``. Both halves of its shape matter.

    (i) The kind is registered with a handler that **would succeed if it were
    entered** — with gate 2 deleted the pass falls straight through to that handler on
    the re-acquire and the job ends ``succeeded``, which is what kills that mutant. An
    unregistered kind or a raising handler would reach ``failed`` by a different route
    and leave it alive. (ii) The assertion on ``last_error`` is gate 2's exact text,
    the only thing that distinguishes this gate from every other route to ``failed``.
    """
    job_id = _put(engine, now=now, max_attempts=1)
    with engine.begin() as conn:
        assert (
            acquire_lease(
                conn, owner="worker-that-died", now=now, lease_seconds=LEASE_SECONDS
            )
            is not None
        )

    lapsed = now + timedelta(seconds=LEASE_SECONDS + 1)
    _visit(workspace, kinds=_registry(), backend=cluster.backend, at=lapsed)

    row = _read(engine, job_id)
    assert row.state == "failed"
    assert row.attempts == 2, "the crash arm costs one extra acquire to observe"
    assert row.last_error == RETRY_BUDGET_EXHAUSTED
    assert row.finished_at == lapsed
    assert _notes(engine) == (), "gate 2 runs before the handler is ever entered"


# --- AC 6, loop half: the loser discards its own effects ------------------------------


def test_a_worker_whose_lease_is_stolen_mid_handler_discards_its_own_effects(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 6, loop half: transaction 2 is rolled back, so only the winner's note lands.

    The steal happens **inside** the handler, after it has already written, which is
    the only ordering that distinguishes a commit-then-check implementation from a
    check-then-commit one.
    """
    job_id = _put(engine, now=now)
    stolen_at = now + timedelta(seconds=LEASE_SECONDS + 1)

    def steals_its_own_lease(
        uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
    ) -> None:
        write_note(uow.connection, body="loser")
        with engine.begin() as conn:
            taken = acquire_lease(
                conn, owner=THIEF, now=stolen_at, lease_seconds=LEASE_SECONDS
            )
            assert taken is not None and taken.id == job_id
            write_note(conn, body="winner")

    _visit(
        workspace,
        kinds=_registry(steals_its_own_lease),
        backend=cluster.backend,
        at=now,
    )

    row = _read(engine, job_id)
    assert row.state == "leased", "the loser must not overwrite the winner's state"
    assert row.lease_owner == THIEF
    assert row.finished_at is None
    assert _notes(engine) == ("winner",), (
        "the loser's effects must be rolled back, not committed beside the winner's"
    )


# --- AC 10 and AC 11: cancellation, in every window it can land in --------------------


def test_a_cancelled_queued_job_is_terminal_before_any_visit(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 10: the handler is never entered and the pass never acquires the row."""
    job_id = _put(engine, now=now)
    with engine.begin() as conn:
        assert request_cancellation(conn, job_id, now=now) == "cancelled"

    result = _visit(workspace, kinds=_registry(), backend=cluster.backend, at=now)

    assert result.jobs_acquired == 0
    row = _read(engine, job_id)
    assert row.state == "cancelled"
    assert row.attempts == 0
    assert _notes(engine) == ()


def test_a_cancellation_observed_at_a_checkpoint_ends_the_job_cancelled(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 11, **checkpoint window**: the token raises ``JobCancelled`` mid-handler."""
    job_id = _put(engine, now=now)

    def checkpoints_after_the_request(
        uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
    ) -> None:
        write_note(uow.connection, body="written before the checkpoint")
        with engine.begin() as conn:
            assert request_cancellation(conn, job_id, now=now) == "leased"
        token.checkpoint()
        write_note(uow.connection, body="never reached")

    _visit(
        workspace,
        kinds=_registry(checkpoints_after_the_request),
        backend=cluster.backend,
        at=now,
    )

    row = _read(engine, job_id)
    assert row.state == "cancelled"
    assert row.finished_at == now
    assert row.lease_owner is None and row.lease_until is None
    assert _notes(engine) == (), "no durable effect survives a cancelled run"


def test_a_cancellation_landing_after_the_last_checkpoint_still_ends_cancelled(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 11, **commit window**: the handler returns normally and
    ``finish_succeeded``'s own predicate is the only thing that catches the flag."""
    job_id = _put(engine, now=now)

    def is_cancelled_after_its_last_checkpoint(
        uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
    ) -> None:
        token.checkpoint()
        write_note(uow.connection, body="written then rolled back")
        with engine.begin() as conn:
            assert request_cancellation(conn, job_id, now=now) == "leased"

    _visit(
        workspace,
        kinds=_registry(is_cancelled_after_its_last_checkpoint),
        backend=cluster.backend,
        at=now,
    )

    row = _read(engine, job_id)
    assert row.state == "cancelled", "not succeeded: the flag was set before the write"
    assert row.finished_at == now
    assert _notes(engine) == ()


def test_a_cancellation_never_observed_by_the_handler_ends_cancelled_not_queued(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 11, **exception window** — the one that tests K4's "or a further retry".

    The request lands while the handler runs and the handler then raises a *generic*
    exception without ever calling ``checkpoint()``, so the token never sees the flag
    at all. The job is comfortably under budget, so the exception arm reaches
    ``requeue_for_retry``, whose ``AND cancel_requested = false`` returns zero rows and
    sends the loop to the re-read. Neither the checkpoint window (which never reaches
    the requeue) nor the commit window (a different predicate, on a job that never
    raised) exercises this path.
    """
    job_id = _put(engine, now=now, max_attempts=3)

    def raises_without_ever_checkpointing(
        uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
    ) -> None:
        write_note(uow.connection, body="written then rolled back")
        with engine.begin() as conn:
            assert request_cancellation(conn, job_id, now=now) == "leased"
        raise RuntimeError("the handler failed after the cancellation landed")

    _visit(
        workspace,
        kinds=_registry(raises_without_ever_checkpointing),
        backend=cluster.backend,
        at=now,
    )

    row = _read(engine, job_id)
    assert row.state == "cancelled", (
        "a cancelled running task must not be requeued for a further retry"
    )
    assert row.next_run_at == now, "no backoff was written"
    assert row.finished_at == now
    assert _notes(engine) == ()


def test_a_cancelled_job_that_is_also_out_of_budget_ends_cancelled_not_failed(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 11's fourth case: the cancelled-and-exhausted interlock.

    At ``max_attempts = 1`` the exception arm's own ``attempts >= max_attempts`` is
    true on this same raise, so this job reaches ``finish_failed`` rather than
    ``requeue_for_retry`` — the branch the exception window above never touches,
    because its job stays under budget. Cancellation still has to win over exhaustion.
    """
    job_id = _put(engine, now=now, max_attempts=1)

    def raises_without_ever_checkpointing(
        uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
    ) -> None:
        write_note(uow.connection, body="written then rolled back")
        with engine.begin() as conn:
            assert request_cancellation(conn, job_id, now=now) == "leased"
        raise RuntimeError("the handler failed after the cancellation landed")

    _visit(
        workspace,
        kinds=_registry(raises_without_ever_checkpointing),
        backend=cluster.backend,
        at=now,
    )

    row = _read(engine, job_id)
    assert row.state == "cancelled", "cancellation wins over exhaustion"
    assert row.attempts == 1
    assert _notes(engine) == ()


# --- the retry arm the requeue path needs proving before AC 15 can rest on it ---------


def test_a_raising_handler_under_budget_is_requeued_with_its_backoff(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """The exception arm's ordinary case.

    Requeued at its backoff, the error recorded, the handler's effects discarded.
    """
    job_id = _put(engine, now=now, max_attempts=3)

    def raises(
        uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
    ) -> None:
        write_note(uow.connection, body="written then rolled back")
        raise RuntimeError("handler raised")

    result = _visit(workspace, kinds=_registry(raises), backend=cluster.backend, at=now)

    row = _read(engine, job_id)
    assert row.state == "queued"
    assert row.next_run_at == now + BACKOFF_FIRST_RETRY
    assert row.last_error == "handler raised"
    assert row.attempts == 1, "no failure path increments attempts"
    assert _notes(engine) == ()
    assert result.next_due_at == now + BACKOFF_FIRST_RETRY


# --- AC 13: an unknown kind and an unparseable payload, both terminal on first sight --


def test_an_unregistered_kind_fails_terminally_rather_than_sitting_queued(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    job_id = _put(engine, now=now, kind="harness.note.unregistered")

    _visit(workspace, kinds=_registry(), backend=cluster.backend, at=now)

    row = _read(engine, job_id)
    assert row.state == "failed"
    assert "harness.note.unregistered" in row.last_error
    assert row.finished_at == now


def test_a_registered_kind_whose_payload_fails_its_model_fails_terminally(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 13's sibling, the no-retry path.

    **The state and the error text are the assertion, not ``attempts``.**
    ``requeue_for_retry`` never touches ``attempts`` — only ``acquire_lease`` does — so
    "``attempts`` unchanged from the acquire" holds whether the job reached
    ``finish_failed`` or was wrongly requeued, and proves nothing about which path was
    taken.
    """
    job_id = _put(engine, now=now, payload={"not_the_field": "at all"})

    _visit(workspace, kinds=_registry(), backend=cluster.backend, at=now)

    row = _read(engine, job_id)
    assert row.state == "failed", "a payload that can never parse is never retried"
    assert KIND in row.last_error
    assert "body" in row.last_error, "the error names the field that failed"
    assert _notes(engine) == ()


# --- AC 15's third clause: the per-visit cap ------------------------------------------


def test_a_visit_stops_at_its_cap_and_reports_itself_due_again_at_once(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """A visitor that stopped early must say so in the index immediately, rather than
    let the workspace wait out the 900 s reconcile floor."""
    for _ in range(MAX_JOBS_PER_VISIT + 1):
        _put(engine, now=now)

    result = _visit(workspace, kinds=_registry(), backend=cluster.backend, at=now)

    assert result.jobs_acquired == MAX_JOBS_PER_VISIT
    assert result.next_due_at == now, "a fresh instant, not the computed value"
    with engine.connect() as conn:
        still_queued = conn.execute(
            select(job.c.id).where(job.c.state == "queued")
        ).all()
    assert len(still_queued) == 1


# --- issue #66 item 1: the outcome instant, on both sides of the handler --------------
#
# Four tests and four mutations, because this defect is pair-shaped and has recurred
# three times: a fix that closes one half and leaves the other shipped once already,
# past five internal gates. Each test below isolates one site, so a regression in either
# half is caught mechanically rather than by re-reading the diff.


def test_a_handler_slower_than_its_backoff_is_requeued_into_the_future(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """#66's **within-job** half: the requeue instant is read after the raise.

    Thirty seconds is more than ``backoff_for(1)``'s five, and that gap is the defect:
    computed from the acquire instant, ``next_run_at`` lands twenty-five seconds in the
    past, the job is due again the moment it is written, and its whole retry budget is
    spent back to back with no delay at all.

    **The assertion is against the instant the handler ended, never against ``now``.**
    Against ``now`` it would also hold for a loop that never refreshed anything, since
    ``now + 5 s`` is in ``now``'s future too.
    """
    job_id = _put(engine, now=now, max_attempts=3)
    clock = AdvancingClock(now)
    ended: list[datetime] = []

    def raises_after_its_own_backoff_elapsed(
        uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
    ) -> None:
        write_note(uow.connection, body="written then rolled back")
        ended.append(clock.advance(LONG_HANDLER))
        raise RuntimeError("handler raised")

    _visit_on(
        workspace,
        kinds=_registry(raises_after_its_own_backoff_elapsed),
        backend=cluster.backend,
        clock=clock,
    )

    row = _read(engine, job_id)
    assert row.state == "queued"
    assert ended == [now + LONG_HANDLER], "the handler ran exactly once"
    assert row.next_run_at > ended[0], (
        "a retry instant computed before the handler ran is already in the past"
    )
    assert row.next_run_at >= ended[0] + BACKOFF_FIRST_RETRY
    assert _notes(engine) == ()


def test_each_job_in_one_visit_takes_its_lease_on_an_instant_of_its_own(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """#66's **between-jobs** half, asserted on leases that are still live.

    Each handler records its own job's ``lease_until`` from a second connection while it
    holds the lease. Reading the two rows back after the visit would compare two
    ``NULL``s — every terminal and requeue write nulls ``lease_until`` — and pass
    whatever the loop did. That vacuous shape is how this half went unguarded before,
    so it is the shape the test deliberately avoids.

    Which of the two jobs runs first is not decidable from here: both are enqueued at
    the same instant, so ``acquire_lease``'s ``ORDER BY next_run_at`` does not order
    them. The clock is advanced by whichever one runs first.
    """
    _put(engine, now=now, payload={"body": "one"})
    _put(engine, now=now, payload={"body": "two"})
    clock = AdvancingClock(now)
    captured: list[Row[Any]] = []

    def records_its_own_live_lease(
        uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
    ) -> None:
        captured.append(_live_lease(engine))
        if len(captured) == 1:
            clock.advance(LONG_HANDLER)

    result = _visit_on(
        workspace,
        kinds=_registry(records_its_own_live_lease),
        backend=cluster.backend,
        clock=clock,
    )

    assert result.jobs_acquired == 2
    assert len(captured) == 2
    assert captured[0].id != captured[1].id, "two jobs, not one row read twice"
    assert captured[1].lease_until - captured[0].lease_until >= LONG_HANDLER, (
        "the second job's lease was taken on the first job's instant"
    )


def test_a_successful_job_is_finished_at_the_instant_its_handler_ended(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """#66 on the path that runs most often: ``finished_at`` is post-handler.

    The success arm writes inside the handler's own transaction, which is what makes it
    the easiest of the three sites to leave on the pre-handler instant by accident.
    """
    job_id = _put(engine, now=now)
    clock = AdvancingClock(now)
    ended: list[datetime] = []

    def works_for_a_measurable_while(
        uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
    ) -> None:
        write_note(uow.connection, body=payload.body)
        ended.append(clock.advance(LONG_HANDLER))

    _visit_on(
        workspace,
        kinds=_registry(works_for_a_measurable_while),
        backend=cluster.backend,
        clock=clock,
    )

    row = _read(engine, job_id)
    assert row.state == "succeeded"
    assert ended == [now + LONG_HANDLER]
    assert row.finished_at == ended[0], "finished_at is not the instant it was leased"
    assert row.lease_owner is None and row.lease_until is None
    assert _notes(engine) == ("done",)


def test_a_visit_that_caps_out_reports_a_post_handler_instant(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """#66's third site: ``visit_workspace``'s cap branch.

    The sibling above pins the branch itself, but on a frozen clock, where the last
    job's acquire instant and the visit's end are the same value. Moving the clock
    inside the last handler is the only thing that separates them — and the index row
    this feeds decides when the workspace is offered again, so reporting the acquire
    instant sends the next pass back before the work it left behind is really due.
    """
    for _ in range(MAX_JOBS_PER_VISIT + 1):
        _put(engine, now=now)
    clock = AdvancingClock(now)
    ended: list[datetime] = []
    runs = 0

    def the_last_of_them_runs_long(
        uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
    ) -> None:
        nonlocal runs
        write_note(uow.connection, body=payload.body)
        runs += 1
        if runs == MAX_JOBS_PER_VISIT:
            ended.append(clock.advance(LONG_HANDLER))

    result = _visit_on(
        workspace,
        kinds=_registry(the_last_of_them_runs_long),
        backend=cluster.backend,
        clock=clock,
    )

    assert result.jobs_acquired == MAX_JOBS_PER_VISIT
    assert ended == [now + LONG_HANDLER]
    assert result.next_due_at is not None
    assert result.next_due_at >= ended[0], (
        "the cap branch reported the last job's acquire instant, not the visit's end"
    )


# --- AC 11: the due instant is the union of both tables -------------------------------


def test_a_workspace_due_only_for_a_delivery_is_never_reported_idle(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 11, the boundary case #46 is about: a pending delivery and no job at all.

    ``visit_workspace`` computes ``next_due_at`` from both tables, so a workspace
    holding only a delivery must be reported due at that delivery's instant. Computing
    it from ``core.job`` alone reports ``None`` — "nothing is due here" — and the
    workspace then waits out the 900 s reconcile floor before anything looks at it
    again, which is a delivery silently delayed rather than a delivery lost, and so
    exactly the kind of defect a passing suite hides.

    The delivery is published **due in the future** so this visit's own drain leaves it
    alone: the assertion is about what the visit *reports*, and a delivery it had
    already drained would report nothing either way. The sibling assertion is the
    control — a workspace with neither is still ``None``, so this test cannot pass by
    a union that reports something unconditionally.
    """
    due_later = now + timedelta(minutes=5)
    with engine.begin() as conn:
        enable_harness_module(conn)
        ensure_consumer_tables(conn)

    empty = _visit(workspace, kinds=_registry(), backend=cluster.backend, at=now)
    assert empty.next_due_at is None, "the control: neither table has anything due"

    ctx = context_for_operator(workspace)
    with open_unit_of_work(ctx) as uow:
        publish(
            ctx,
            uow,
            NewEvent(
                type=EVENT_TYPE,
                schema_version=1,
                subject_ref="harness.note:union",
                subject_revision=1,
                data={"body": "due later"},
            ),
            now=due_later,
            consumers=consumer_registry(),
        )
        uow.commit()

    result = _visit(
        workspace,
        kinds=_registry(),
        backend=cluster.backend,
        at=now,
        consumers=consumer_registry(),
    )

    assert result.jobs_acquired == 0
    assert result.next_due_at == due_later, (
        "a workspace whose only due work is a delivery was reported as idle; "
        "next_due_at is the earlier of the job side and the delivery side"
    )


# --- AC 14: discovery comes from the index and from nowhere else ----------------------


def test_a_pass_opens_only_the_workspaces_the_due_work_index_offers(
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    fresh_backend: PostgresBackend,
    only_workspaces: Callable[..., None],
    now: datetime,
) -> None:
    """AC 14: an active workspace whose index row is due later is not opened at all.

    Asserted against a **fresh** engine pool, so ``cached()`` names exactly the
    databases this pass opened.
    """
    due = make_workspace()
    later = make_workspace()
    only_workspaces(due, later)
    with cluster.backend.control_engine.begin() as control:
        _set_due_at(control, later, now + timedelta(minutes=10))

    result = run_one_pass(
        kinds=_registry(),
        consumers=ConsumerRegistry(),
        backend=fresh_backend,
        owner=OWNER,
        now=now,
        clock=lambda: now,
        jitter=None,
        reconcile_seconds=RECONCILE_SECONDS,
    )

    cached = fresh_backend.pools.cached()
    assert result.workspaces_visited == 1
    assert cluster.registry_row(due).database_name in cached
    assert cluster.registry_row(later).database_name not in cached, (
        "a workspace due later must not be opened, let alone drained"
    )


# --- AC 15: what each visit writes back to the index ----------------------------------


def test_each_visit_records_the_workspace_at_its_earliest_remaining_work(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    fresh_backend: PostgresBackend,
    only_workspaces: Callable[..., None],
    now: datetime,
) -> None:
    """AC 15: the computed ``next_due_at``, carried through ``record_visit``.

    The job's handler raises, so it is requeued five seconds out and that is the
    earliest remaining work. ``record_visit`` writes ``min(next_due_at, floor)`` and
    the floor is 900 s away, so the five-second value is what lands.
    """
    only_workspaces(workspace)

    def raises(
        uow: HandlerUnitOfWork, payload: NotePayload, token: CancellationToken
    ) -> None:
        raise RuntimeError("handler raised")

    job_id = enqueue(workspace, kind=KIND, payload={"body": "x"}, now=now)

    run_one_pass(
        kinds=_registry(raises),
        consumers=ConsumerRegistry(),
        backend=fresh_backend,
        owner=OWNER,
        now=now,
        clock=lambda: now,
        jitter=None,
        reconcile_seconds=RECONCILE_SECONDS,
    )

    assert _read(engine, job_id).next_run_at == now + BACKOFF_FIRST_RETRY
    assert _due_at(cluster, workspace) == now + BACKOFF_FIRST_RETRY


def test_a_drained_workspace_is_recorded_at_the_reconcile_floor(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    fresh_backend: PostgresBackend,
    only_workspaces: Callable[..., None],
    now: datetime,
) -> None:
    """AC 15's ``None`` arm: nothing remains, so only the reconcile floor is left."""
    only_workspaces(workspace)
    job_id = enqueue(workspace, kind=KIND, payload={"body": "done"}, now=now)

    run_one_pass(
        kinds=_registry(),
        consumers=ConsumerRegistry(),
        backend=fresh_backend,
        owner=OWNER,
        now=now,
        clock=lambda: now,
        jitter=None,
        reconcile_seconds=RECONCILE_SECONDS,
    )

    assert _read(engine, job_id).state == "succeeded"
    due_at = _due_at(cluster, workspace)
    assert due_at is not None
    assert due_at > now + timedelta(seconds=RECONCILE_SECONDS - 120)


# --- AC 16: one sweep per pass, not one per workspace ---------------------------------


def test_close_idle_is_called_exactly_once_per_pass(
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    fresh_backend: PostgresBackend,
    only_workspaces: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
) -> None:
    """AC 16, across a pass touching more than one workspace."""
    first = make_workspace()
    second = make_workspace()
    only_workspaces(first, second)
    swept = 0
    real_close_idle = fresh_backend.pools.close_idle

    def counting_close_idle() -> int:
        nonlocal swept
        swept += 1
        return real_close_idle()

    monkeypatch.setattr(fresh_backend.pools, "close_idle", counting_close_idle)

    result = run_one_pass(
        kinds=_registry(),
        consumers=ConsumerRegistry(),
        backend=fresh_backend,
        owner=OWNER,
        now=now,
        clock=lambda: now,
        jitter=None,
        reconcile_seconds=RECONCILE_SECONDS,
    )

    assert result.workspaces_visited == 2
    assert swept == 1, "once after every workspace in the pass, never once per visit"


# --- AC 18: surviving a failing workspace, a failing pass, and a stop signal ----------


def test_a_workspace_that_fails_past_routing_is_skipped_and_backed_off(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    fresh_backend: PostgresBackend,
    only_workspaces: Callable[..., None],
    now: datetime,
) -> None:
    """AC 18's failing-visit half, failing **past** routing.

    ``workspaces_with_due_work`` already filters ``state = 'active'``, so a
    ``workspace_unavailable`` refusal is reachable only by a race between the index
    read and the visit. The injected raise comes from inside the job loop instead,
    where every failure a first run actually produces lands — a guard narrowed to the
    routing call would let it escape and end the pass.
    """

    class ExplodingRegistry(JobKindRegistry):
        def lookup(self, kind: str) -> Any:
            raise RuntimeError("the job loop failed past routing")

    only_workspaces(workspace)
    enqueue(workspace, kind=KIND, payload={"body": "x"}, now=now)

    result = run_one_pass(
        kinds=ExplodingRegistry(),
        consumers=ConsumerRegistry(),
        backend=fresh_backend,
        owner=OWNER,
        now=now,
        clock=lambda: now,
        jitter=None,
        reconcile_seconds=RECONCILE_SECONDS,
    )

    assert result.workspaces_visited == 1, "the pass survived the failing workspace"
    assert _due_at(cluster, workspace) == now + timedelta(seconds=LEASE_SECONDS), (
        "a failed visit must be recorded, or it is re-offered every second for ever"
    )


def test_a_failing_pass_is_never_fatal_and_backs_off_to_its_ceiling(
    cluster: ClusterSession, monkeypatch: pytest.MonkeyPatch, now: datetime
) -> None:
    """AC 18's failing-pass half: the first-run path, when the index does not exist yet.

    Eight consecutive failures, so the capped doubling is observed reaching its
    ceiling and staying there rather than only at the floor.
    """

    def unavailable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("control.workspace_work_due does not exist yet")

    monkeypatch.setattr(loop_module, "workspaces_with_due_work", unavailable)
    stop = RecordingStop()

    worker_loop(
        kinds=_registry(),
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        stop=stop,
        max_passes=8,
        clock=lambda: now,
    )

    assert stop.waits == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    assert stop.waits[-1] == MAX_PASS_BACKOFF_SECONDS


def test_the_pass_backoff_resets_on_the_first_pass_that_succeeds(
    cluster: ClusterSession, monkeypatch: pytest.MonkeyPatch, now: datetime
) -> None:
    """The reset is the assertion: without it the fifth wait would be 8 s, not 1 s."""
    script = iter(["fail", "fail", "fail", "ok", "fail"])

    def scripted(*args: object, **kwargs: object) -> tuple[DueWorkspace, ...]:
        if next(script) == "fail":
            raise RuntimeError("control.workspace_work_due does not exist yet")
        return ()

    monkeypatch.setattr(loop_module, "workspaces_with_due_work", scripted)
    stop = RecordingStop()

    worker_loop(
        kinds=_registry(),
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        stop=stop,
        max_passes=5,
        clock=lambda: now,
    )

    assert stop.waits == [1.0, 2.0, 4.0, IDLE_INTERVAL_SECONDS, 1.0]


def test_the_loop_exits_on_its_stop_event_with_no_pass_bound(
    cluster: ClusterSession, monkeypatch: pytest.MonkeyPatch, now: datetime
) -> None:
    """AC 18's exit half, with ``max_passes=None`` so only the event can end it."""
    passes = 0

    def empty(*args: object, **kwargs: object) -> tuple[DueWorkspace, ...]:
        nonlocal passes
        passes += 1
        return ()

    monkeypatch.setattr(loop_module, "workspaces_with_due_work", empty)

    class StopsDuringItsFirstWait(RecordingStop):
        def wait(self, timeout: float | None = None) -> bool:
            self.set()
            return super().wait(timeout)

    stop = StopsDuringItsFirstWait()
    worker_loop(
        kinds=_registry(),
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        stop=stop,
        clock=lambda: now,
    )

    assert passes == 1
    assert stop.waits == [IDLE_INTERVAL_SECONDS]

    already_set = RecordingStop()
    already_set.set()
    worker_loop(
        kinds=_registry(),
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        stop=already_set,
        max_passes=5,
        clock=lambda: now,
    )
    assert passes == 1, "a stop event set before entry runs no pass at all"
    assert already_set.waits == []


def test_a_pass_that_found_work_loops_again_without_waiting(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    fresh_backend: PostgresBackend,
    only_workspaces: Callable[..., None],
    now: datetime,
) -> None:
    """``worker_loop`` end to end, on its own generated owner: a busy worker never
    sleeps between passes."""
    only_workspaces(workspace)
    job_id = enqueue(workspace, kind=KIND, payload={"body": "done"}, now=now)
    stop = RecordingStop()

    worker_loop(
        kinds=_registry(),
        consumers=ConsumerRegistry(),
        backend=fresh_backend,
        stop=stop,
        max_passes=1,
        clock=lambda: now,
    )

    assert _read(engine, job_id).state == "succeeded"
    assert _notes(engine) == ("done",)
    assert stop.waits == [], "a pass that found work loops immediately"


def test_install_stop_signals_wires_sigterm_to_the_loops_stop_event() -> None:
    """AC 18's ``SIGTERM`` half — the only test in the run that exercises the wiring.

    The process's prior handlers are saved and restored: ``install_stop_signals``
    replaces them for the rest of the process, and left unrestored that breaks Ctrl-C
    for the remainder of the pytest session. ``SIGINT`` is asserted by identity rather
    than delivered, because a delivered ``SIGINT`` on a broken implementation would
    raise ``KeyboardInterrupt`` and end the session instead of failing one test.
    """
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    try:
        stop = threading.Event()
        install_stop_signals(stop)
        assert signal.getsignal(signal.SIGINT) is signal.getsignal(signal.SIGTERM), (
            "both signals must reach the same stop event"
        )

        os.kill(os.getpid(), signal.SIGTERM)

        assert stop.wait(5.0), "SIGTERM must set the event worker_loop waits on"
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
