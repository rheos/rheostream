"""Every read and write against ``core.job``: the lease, the four finishes, the
cancellation request, and the two queries a worker and a failure list need.

Shaped after ``storage/work_index.py``. Each function takes the caller's
``Connection`` and runs inside the caller's transaction, and **none of them commits**.
``enqueue`` is the single exception and says so in its own docstring. ``now`` is always
an explicit keyword parameter: nothing here reads the process clock, so a test drives
lease expiry by choosing an instant rather than by sleeping.

**``:now`` is a bound parameter, never SQL ``now()``.** One parameter, chosen by the
caller, drives every predicate of the one statement it appears in, so the lease query's
two ``WHERE`` arms can never disagree with each other. Server-side ``now()`` would also
be uncontrollable from a test and would force real sleeps to prove expiry. A later
reader will want to "fix" this back; the cost of doing so is in the next paragraph.

*The multi-host assumption that carries.* With ``:now`` bound from each caller's own
clock, two workers on hosts skewed by more than ``lease_seconds`` minus the checkpoint
cadence can steal each other's leases. Release one's reference topology is a
single-host compose stack, so this is sound today; a multi-host deployment needs either
synchronised clocks or a move back to server-side ``now()``.

**Four deviations from ``docs/architecture/intake-and-events.md`` § Jobs and the
worker**, recorded here so a reviewer finds them without re-deriving them:

1. **``:now`` is a bound parameter** rather than SQL ``now()`` — above.
2. **``attempts`` is incremented under a ``CASE``**, not unconditionally. See
   :func:`acquire_lease`.
3. **``ORDER BY next_run_at``.** The ratified statement gives the job query no
   ordering. Jobs have no ``position`` column and no ordering requirement, so
   oldest-due-first is a choice rather than a transcription: nothing starves behind a
   steady arrival rate.
4. **Two transactions per attempt, plus short heartbeat transactions**, where the
   document's worker step reads as one. The acquire commits on its own, because a lease
   that sat uncommitted for the length of the handler would be invisible to a competing
   worker; the handler's effects and the row's terminal state commit together, so a
   job's effects and its success are never separable; and any non-success outcome rolls
   that transaction back and writes the terminal or requeued state in a second one.
   :class:`~rheo_core.work.cancellation.CancellationToken` opens the short ones.

**Why the conditional writes are conditional.** Every terminal and requeue write is
predicated on ``id = :id AND lease_owner = :owner AND state = 'leased'`` (:func:`_held`)
and returns whether it applied. Without the ownership half, a worker whose lease expired
mid-handler overwrites the state the *new* owner set — the worst silent defect available
here. Three of the four carry ``AND cancel_requested = false`` on top of that, and each
call site's docstring names the window it closes; a zero rowcount from any of the three
means the same thing, stated once on :func:`finish_succeeded`.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import Connection, and_, case, func, insert, or_, select, update
from sqlalchemy.sql.elements import ColumnElement

from rheo_core.refs import uuid7
from rheo_core.settings import resolve
from rheo_core.storage import work_tables as t
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.routing import active_workspace
from rheo_core.storage.work_index import mark_work_due

QUEUED: Final = "queued"
LEASED: Final = "leased"
SUCCEEDED: Final = "succeeded"
FAILED: Final = "failed"
CANCELLED: Final = "cancelled"
"""The five members of the ``job_state`` check constraint (``work_tables.JOB_STATES``),
named here so no predicate in this module spells one of them by hand."""


@dataclass(frozen=True, slots=True)
class LeasedJob:
    """The row :func:`acquire_lease` just took, as the worker needs to see it.

    ``payload`` is the row's ``input`` column deserialized; typed
    ``dict[str, object]`` rather than a bare ``dict`` because the root
    ``pyproject.toml`` sets mypy ``strict``, and because that is the shape the rest of
    the repository already uses for a JSON-like value whose contents are a later
    concern.

    ``operation_id`` is the row's own column, carried on the lease rather than re-read
    at each terminal write. The worker terminalises a job's ``core.operation`` record
    in the same transaction as the job's terminal write, and there are six such
    writes; a value that never changes after :func:`enqueue_job` wrote it is read once,
    by the acquire that already returns five other columns, rather than six more times
    under six separate transactions.
    """

    id: UUID
    kind: str
    payload: dict[str, object]
    attempts: int
    max_attempts: int
    cancel_requested: bool
    operation_id: UUID | None


@dataclass(frozen=True, slots=True)
class FailedJobRow:
    """One row of the failure list, as the database holds it.

    Six fields, deliberately: they are exactly what the read operation over this list
    publishes, so its handler maps one of these to one output model field for field
    and never reaches back into this module for anything else. A plain frozen
    dataclass with no pydantic and no operations import — this module stays free of
    any dependency on the operation package.
    """

    id: UUID
    kind: str
    attempts: int
    max_attempts: int
    last_error: str | None
    finished_at: datetime | None


def _held(job_id: UUID, owner: str) -> ColumnElement[bool]:
    """The predicate every terminal and requeue write shares.

    One helper rather than four hand-written copies: four is the shape a later edit
    updates in three places.
    """
    return and_(
        t.job.c.id == job_id,
        t.job.c.lease_owner == owner,
        t.job.c.state == LEASED,
    )


def _apply(conn: Connection, where: ColumnElement[bool], **values: object) -> bool:
    """Run one predicated ``UPDATE`` on ``core.job`` and report whether it matched."""
    return conn.execute(update(t.job).where(where).values(**values)).rowcount == 1


def enqueue_job(
    conn: Connection,
    *,
    kind: str,
    payload: dict[str, object],
    now: datetime,
    max_attempts: int,
    next_run_at: datetime | None = None,
    operation_id: UUID | None = None,
) -> UUID:
    """Write one ``queued`` row and return its id. Does not commit.

    ``attempts = 0``, ``cancel_requested = false``, and ``next_run_at`` defaults to
    ``now`` — a job with no requested delay is due the moment its transaction commits.

    ``operation_id`` is the first writer ``job.operation_id`` has ever had. Additive
    with a ``None`` default, so both existing callers — :func:`enqueue` below and
    ``tests/postgres/test_job_repository.py`` — need no edit. A non-``None`` value
    ties the job to a ``core.operation`` record the dispatcher has already committed,
    and is what makes the worker terminalise that record when this job finishes; a
    ``long_running`` handler reads it from ``HandlerUnitOfWork.operation_id`` and
    passes it here.
    """
    job_id = uuid7()
    conn.execute(
        insert(t.job).values(
            id=job_id,
            kind=kind,
            state=QUEUED,
            input=payload,
            operation_id=operation_id,
            attempts=0,
            max_attempts=max_attempts,
            next_run_at=now if next_run_at is None else next_run_at,
            cancel_requested=False,
            created_at=now,
        )
    )
    return job_id


def enqueue(
    workspace_id: UUID,
    *,
    kind: str,
    payload: dict[str, object],
    now: datetime,
    max_attempts: int | None = None,
) -> UUID:
    """The production enqueue: write the job, **commit**, then mark the workspace due.

    **The one function in this module that commits**, and it must. The job row lands in
    the workspace database and the due-work mark lands in the control database; no
    transaction spans the two (``storage/work_index.py``'s own docstring makes the same
    point), so the workspace write commits first and the mark follows it. A crash
    between them loses the mark, which is bounded by the reconcile floor rather than
    fixed by a transaction that cannot exist.

    ``max_attempts`` omitted resolves to ``work.max_attempts``. **``get_int``, never
    the mapping subscript:** ``ResolvedSettings.__getitem__`` returns the
    ``SettingValue`` union, which does not flow into an ``int`` column write under
    mypy strict, so the subscript form fails the typecheck gate rather than a test.
    This resolution is that key's only production reader.
    """
    budget = (
        resolve().get_int("work.max_attempts") if max_attempts is None else max_attempts
    )
    row = active_workspace(workspace_id)
    engine = get_backend().pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        job_id = enqueue_job(
            uow.connection,
            kind=kind,
            payload=payload,
            now=now,
            max_attempts=budget,
        )
        uow.commit()
    with get_backend().control_engine.begin() as control:
        mark_work_due(control, workspace_id, at=now)
    return job_id


def acquire_lease(
    conn: Connection, *, owner: str, now: datetime, lease_seconds: int
) -> LeasedJob | None:
    """Lease at most one due job for ``owner``, or ``None`` when nothing is due.

    ``FOR UPDATE SKIP LOCKED`` on the inner select is what makes two workers take two
    different rows instead of one blocking on the other. The second ``WHERE`` arm — a
    ``leased`` row whose ``lease_until`` has passed — **is** the crash-recovery path: an
    expired lease is a dead worker, and re-acquiring counts the attempt.

    **``attempts`` is incremented in exactly this one place.** No failure path
    increments it.

    **The ``CASE`` (deviation 2).** The architecture increments unconditionally. This
    narrows to: an expired-leased row whose cancellation landed while its previous
    owner was running. Counting an attempt against a job about to be *cancelled* rather
    than retried would make the data contradict the terminal state.

    That such a row can only reach the second arm is a property of **two** writes
    agreeing, not of one: :func:`request_cancellation` never leaves a ``queued`` row
    with the flag set, and :func:`requeue_for_retry` carries its own
    ``cancel_requested`` predicate so it never writes one either. Remove the second of
    those and a cancellation landing on a ``leased`` row whose handler then raises
    produces exactly the row this forbids — ``queued``, flagged, acquirable on the
    **first** arm. The ``CASE`` stays regardless of either producer: it is what keeps
    such a row harmless if one of them is ever changed, and neither half is a comment
    on the other.
    """
    due = (
        select(t.job.c.id)
        .where(
            or_(
                and_(t.job.c.state == QUEUED, t.job.c.next_run_at <= now),
                and_(t.job.c.state == LEASED, t.job.c.lease_until < now),
            )
        )
        .order_by(t.job.c.next_run_at)
        .with_for_update(skip_locked=True)
        .limit(1)
        .scalar_subquery()
    )
    row = conn.execute(
        update(t.job)
        .where(t.job.c.id == due)
        .values(
            state=LEASED,
            lease_owner=owner,
            lease_until=now + timedelta(seconds=lease_seconds),
            attempts=case(
                (t.job.c.cancel_requested, t.job.c.attempts),
                else_=t.job.c.attempts + 1,
            ),
        )
        .returning(
            t.job.c.id,
            t.job.c.kind,
            t.job.c.input,
            t.job.c.attempts,
            t.job.c.max_attempts,
            t.job.c.cancel_requested,
            t.job.c.operation_id,
        )
    ).one_or_none()
    if row is None:
        return None
    return LeasedJob(
        id=row.id,
        kind=str(row.kind),
        payload=dict(row.input),
        attempts=int(row.attempts),
        max_attempts=int(row.max_attempts),
        cancel_requested=bool(row.cancel_requested),
        operation_id=row.operation_id,
    )


def extend_lease(
    conn: Connection, *, job_id: UUID, owner: str, now: datetime, lease_seconds: int
) -> bool | None:
    """Push ``lease_until`` out while the handler runs, and read the flag back.

    ``None`` means zero rows — the lease is no longer this owner's ``leased`` row — and
    is the only way this function reports that. Otherwise the returned value is
    ``cancel_requested`` as the row now holds it, which is what makes one round trip
    serve as both a heartbeat and a cancellation read.

    Its caller (:class:`~rheo_core.work.cancellation.CancellationToken`) opens and
    commits the short transaction this runs in; this function only executes the
    statement.
    """
    flag = conn.execute(
        update(t.job)
        .where(_held(job_id, owner))
        .values(lease_until=now + timedelta(seconds=lease_seconds))
        .returning(t.job.c.cancel_requested)
    ).scalar_one_or_none()
    return None if flag is None else bool(flag)


def finish_succeeded(
    conn: Connection, *, job_id: UUID, owner: str, now: datetime
) -> bool:
    """End the job ``succeeded``. Returns whether the write applied.

    **``AND cancel_requested = false``, for "rather than a success".** Without it a
    cancellation landing between the handler's last ``checkpoint()`` and this write
    still commits ``succeeded`` with the flag set. The predicate closes that window at
    the commit point rather than at the last checkpoint.

    **What a zero rowcount means, once, for all three of the writes that carry this
    predicate.** The caller rolls its work transaction back unconditionally — a
    faithful implementation that commits first and checks the rowcount afterwards ships
    duplicated effects on every lease theft — and then re-reads the row in a fresh
    short transaction: still ours, still ``leased``, flag set -> write
    :func:`finish_cancelled`; anything else -> the lease is genuinely lost. There is
    one zero-rowcount branch, not three.
    """
    return _apply(
        conn,
        _held(job_id, owner) & t.job.c.cancel_requested.is_(False),
        state=SUCCEEDED,
        finished_at=now,
        lease_owner=None,
        lease_until=None,
        last_error=None,
    )


def finish_failed(
    conn: Connection, *, job_id: UUID, owner: str, now: datetime, error: str
) -> bool:
    """End the job ``failed`` with ``error``. Returns whether the write applied.

    **``AND cancel_requested = false``, for the arm that reaches exhaustion instead of
    a checkpoint.** A job cancelled before it was ever leased never gets here — the
    pre-handler cancellation gate runs before the budget check — so what this closes is
    narrower: a **running** job is cancelled (flag only) while it holds the lease, its
    handler then raises a generic exception on what happens to be its last attempt
    without ever reaching a ``checkpoint()``, so nothing observed the flag before the
    exception arm decided the budget was spent. Without the predicate that job ends
    ``failed`` instead of ``cancelled``, and cancellation is supposed to win over
    exhaustion.
    """
    return _apply(
        conn,
        _held(job_id, owner) & t.job.c.cancel_requested.is_(False),
        state=FAILED,
        finished_at=now,
        lease_owner=None,
        lease_until=None,
        last_error=error,
    )


def finish_cancelled(
    conn: Connection, *, job_id: UUID, owner: str, now: datetime
) -> bool:
    """End the job ``cancelled``. Returns whether the write applied.

    The one terminal write with no ``cancel_requested`` predicate: it is the write the
    other three defer to, and predicating it on the flag it exists to honour would make
    it unreachable from the re-read branch in the one case that matters.
    """
    return _apply(
        conn,
        _held(job_id, owner),
        state=CANCELLED,
        finished_at=now,
        lease_owner=None,
        lease_until=None,
    )


def requeue_for_retry(
    conn: Connection,
    *,
    job_id: UUID,
    owner: str,
    now: datetime,
    next_run_at: datetime,
    error: str,
) -> bool:
    """Put the job back on the queue at ``next_run_at``. Returns whether it applied.

    ``now`` is unused by the statement and kept in the signature so every finish reads
    the same at its call site.

    **``AND cancel_requested = false``, for "or a further retry".** Without it: a leased
    job is cancelled (flag only), its handler then raises a generic exception without
    ever reaching a checkpoint, the work transaction is rolled back, and this write puts
    the row back to ``queued`` with the flag still set under a backoff of up to an hour
    — a cancelled running task requeued for a further retry.
    """
    return _apply(
        conn,
        _held(job_id, owner) & t.job.c.cancel_requested.is_(False),
        state=QUEUED,
        next_run_at=next_run_at,
        lease_owner=None,
        lease_until=None,
        last_error=error,
    )


def request_cancellation(
    conn: Connection, job_id: UUID, *, now: datetime
) -> str | None:
    """Ask for ``job_id`` to be cancelled; return its resulting state, or ``None``.

    One statement, two behaviours, and the split is the point:

    * A ``queued`` row goes **straight to ``cancelled``** in this same write. Nothing
      holds a lease on a queued row, so there is nothing to race, and terminalising now
      is what keeps a recorded terminal status from being up to an hour late for a job
      sitting under a full backoff.
    * A ``leased`` row gets ``cancel_requested = true`` **only**. A worker owns the row
      and is the only thing that may end it; its handler sees the flag at its next
      checkpoint, or the commit-point predicate catches it.

    Any other state is already terminal and matches nothing, so a request against a
    finished job is a no-op and never a state regression.

    Both interleavings against a concurrent :func:`acquire_lease` resolve: if the
    acquire takes the row lock first this ``UPDATE`` blocks, then re-evaluates against
    ``state = 'leased'`` and takes the flag-only branch; if this statement wins, the row
    is ``cancelled`` and the acquire's inner select no longer matches it.
    """
    was_queued = t.job.c.state == QUEUED
    state = conn.execute(
        update(t.job)
        .where(t.job.c.id == job_id)
        .where(t.job.c.state.in_((QUEUED, LEASED)))
        .values(
            state=case((was_queued, CANCELLED), else_=t.job.c.state),
            finished_at=case((was_queued, now), else_=t.job.c.finished_at),
            cancel_requested=True,
        )
        .returning(t.job.c.state)
    ).scalar_one_or_none()
    return None if state is None else str(state)


def list_failed_jobs(conn: Connection, *, limit: int) -> tuple[FailedJobRow, ...]:
    """The most recently failed jobs, newest ``finished_at`` first, capped at
    ``limit``."""
    rows = conn.execute(
        select(
            t.job.c.id,
            t.job.c.kind,
            t.job.c.attempts,
            t.job.c.max_attempts,
            t.job.c.last_error,
            t.job.c.finished_at,
        )
        .where(t.job.c.state == FAILED)
        .order_by(t.job.c.finished_at.desc())
        .limit(limit)
    )
    return tuple(
        FailedJobRow(
            id=row.id,
            kind=str(row.kind),
            attempts=int(row.attempts),
            max_attempts=int(row.max_attempts),
            last_error=None if row.last_error is None else str(row.last_error),
            finished_at=row.finished_at,
        )
        for row in rows
    )


def earliest_due_at(conn: Connection) -> datetime | None:
    """When this workspace next needs a visit, or ``None`` when it needs none.

    The earlier of the soonest queued ``next_run_at`` and the soonest leased
    ``lease_until``: the first is work waiting to start, the second is the moment a
    lease lapses and its job becomes re-acquirable. ``LEAST`` ignores a null side, so
    a workspace with only one of the two still answers. Terminal rows are neither.
    """
    value = conn.execute(
        select(
            func.least(
                func.min(t.job.c.next_run_at).filter(t.job.c.state == QUEUED),
                func.min(t.job.c.lease_until).filter(t.job.c.state == LEASED),
            )
        )
    ).scalar_one_or_none()
    return value if isinstance(value, datetime) else None
