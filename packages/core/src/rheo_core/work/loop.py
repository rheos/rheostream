"""The worker: one process loop, one pass, one workspace visit, one unit of work at a
time — a job, and then a delivery.

Three functions, each the unit the one above it repeats. :func:`worker_loop` is the
process: it owns the stop event, the once-per-pass settings read and the back-off
between passes. :func:`run_one_pass` is one sweep of the due-work index: the
control-plane read, the visits, the ``record_visit`` write per workspace, and one
``close_idle`` at the end. :func:`visit_workspace` drains up to
:data:`MAX_JOBS_PER_VISIT` jobs out of one workspace database, and then up to the
same bound of event deliveries.

**The transaction map, because it is the run's biggest call.** Per job: the acquire is
its own transaction and commits immediately, because a lease that sat uncommitted for
the length of the handler would be invisible to a competing worker. The handler's
effects and the row's ``succeeded`` write share a second transaction and commit
together, so a job's effects and its success are never separable. Every non-success
outcome rolls that second transaction back and writes the terminal or requeued state in
a fresh third one. ``CancellationToken`` opens its own short transactions on top.

**A job carrying a ``core.operation`` id adds one write to whichever transaction ends
it, and never a transaction of its own.** The sites that put a job row into a terminal
state are this function's two pre-handler gates, :func:`_fail_terminally`,
:func:`_run_handler`'s success path, :func:`_write_failure_outcome`'s exhausted branch
and :func:`_resolve_zero_rowcount`'s cancellation branch, and each of them writes the
operation record's terminal state beside the job's, under the same commit. The gates,
:func:`_fail_terminally` and the exhausted branch go through :func:`_with_operation`,
which is the shape they share; the success path and :func:`_resolve_zero_rowcount`
write it inline, because each already sits inside a transaction it opened for other
reasons and has nothing to hand that helper. The requeue branch ends nothing and writes
nothing: see :func:`_write_failure_outcome`.

**A job cancelled while still ``queued`` never reaches this module at all**, and its
record is terminalised by ``work.jobs.request_cancellation`` instead — that statement
is where such a job finishes, no worker ever leases it, and none of the sites above
runs. That function's own docstring carries the reason.

**Per delivery the map is the same three transactions, and the middle one carries one
write more.** The lease commits alone; the consumer's own effects, the
``core.consumer_processed`` ledger row and the delivery's ``delivered`` write share the
second and commit together, which is what makes a delivery exactly-once rather than
at-least-once; a raise rolls all three back and the requeue or the terminal ``failed``
is written in a fresh third. Nothing on this side has a cancellation checkpoint, so
there is no fourth.

**The clock is injected, and there are two of them by design.**
:func:`run_one_pass` holds one pass-level ``now`` that drives the due-work index only;
:func:`visit_workspace` takes no ``now`` at all and calls ``clock()`` fresh at the top
of every per-job iteration, and again at every outcome written after a handler ran —
a handler's duration is unbounded, so its acquire instant is not the instant it
finished. A pass can run for seconds, and a single instant captured at pass start
would be stale by the time it were written as a lease expiry or a retry's
``next_run_at``. See each function's own docstring.

**The controlled clock is wall-clock-anchored, not an arbitrary instant. This is a
constraint on every caller, tests included.** ``record_visit`` and ``mark_work_due``
(both in ``storage/work_index.py``, neither of them this module's to edit) read
``datetime.now(UTC)`` internally: ``record_visit`` computes its floor as ``now() +
work.due_reconcile_seconds`` and writes ``min(next_due_at, floor)``, and
``mark_work_due`` stamps ``updated_at`` from it. So a ``now`` handed to this module that
is in the *past*, or more than ``work.due_reconcile_seconds`` (900 s) into the future,
makes the resulting index rows wrong **on otherwise correct code** — the symptom is a
flaky due-work assertion and the cause is three files away. A controlled clock here
means ``datetime.now(UTC) + timedelta(...)``: a delta from the real clock, never a
chosen instant.
"""

import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ValidationError
from rheo_contracts import EventEnvelope
from sqlalchemy import Connection, Engine, select

from rheo_core.events import (
    ConsumerHandler,
    ConsumerRegistry,
    ConsumerUnknown,
    LeasedDelivery,
    already_processed,
    earliest_delivery_due_at,
    fail_delivery,
    lease_delivery,
    mark_delivered,
    record_processed,
    requeue_delivery,
)
from rheo_core.operations import records as operation_records
from rheo_core.refs import uuid7
from rheo_core.settings import resolve
from rheo_core.storage import work_tables as t
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.postgres import PostgresBackend, get_backend
from rheo_core.storage.routing import active_workspace
from rheo_core.storage.work_index import (
    DueWorkspace,
    record_visit,
    workspaces_with_due_work,
)
from rheo_core.work.backoff import backoff_for
from rheo_core.work.cancellation import CancellationToken, JobCancelled, JobLeaseLost
from rheo_core.work.jobs import (
    LEASED,
    LeasedJob,
    acquire_lease,
    earliest_due_at,
    finish_cancelled,
    finish_failed,
    finish_succeeded,
    requeue_for_retry,
)
from rheo_core.work.kinds import JobKindRegistry, JobKindUnknown
from rheo_core.work.scheduled_authority import verified_execution_for
from rheo_core.work.schedules import earliest_schedule_due_at, run_due_schedules

LEASE_SECONDS: Final = 60
"""``docs/architecture/intake-and-events.md`` § Jobs and the worker, step 1."""

HEARTBEAT_SECONDS: Final = 20
"""Same section, step 2. Supplied to ``CancellationToken``, which cannot import it."""

IDLE_INTERVAL_SECONDS: Final = 1.0
"""Same section's "one-second idle interval"."""

MAX_JOBS_PER_VISIT: Final = 16
"""A chosen bound with no architecture citation behind it, unlike the three above:
the spec's own "Rejected as unforced" list names it — *hardcode 16, promote when an
operator asks*."""

MAX_PASS_BACKOFF_SECONDS: Final = 60.0
"""The ceiling of :func:`worker_loop`'s capped doubling after consecutive failed
passes."""

RETRY_BUDGET_EXHAUSTED: Final = (
    "retry budget exhausted: lease expired without a recorded outcome"
)
"""The pre-handler budget gate's error text, on **both** drains. Fixed, because it is
the only thing that distinguishes the crash arm's ``failed`` from every other route to
``failed``: a dead worker writes nothing, so this string is the whole of the record
that a budget was spent by crashing rather than by raising."""

JOB_FAILED: Final = "job_failed"
"""``operation.error_code`` for a record this module terminalises ``failed``.

One code for all four routes to ``failed`` — the exception arm's spent budget, the
crash arm's, an unknown job kind and a payload that cannot parse — because the code
names *what* failed and all four are the same thing: the job carrying the operation.
Which of the four it was is in ``error_text``, which is the job's own ``last_error``
verbatim, so nothing is lost by not spelling it twice."""

logger = logging.getLogger("rheo_core.work")

if HEARTBEAT_SECONDS >= LEASE_SECONDS:  # pragma: no cover - a build-time invariant
    raise RuntimeError(
        f"HEARTBEAT_SECONDS ({HEARTBEAT_SECONDS}) must be shorter than LEASE_SECONDS "
        f"({LEASE_SECONDS}): a heartbeat no more frequent than the lease cannot keep "
        "it alive, and every handler longer than one lease would be treated as dead"
    )


@dataclass(frozen=True, slots=True)
class VisitResult:
    """What one workspace visit reports back to the pass that called it.

    ``next_due_at`` is the value the caller hands ``record_visit``; ``jobs_acquired``
    counts leases taken, including the ones the pre-handler gates terminalised without
    entering a handler, because those are work the pass did. ``deliveries_acquired``
    counts the same thing on the delivery side, and exists for the same reason the
    job count does: a visit that leased only deliveries did work, and a pass that
    cannot see that sleeps through work it has already found.
    """

    next_due_at: datetime | None
    jobs_acquired: int
    deliveries_acquired: int


@dataclass(frozen=True, slots=True)
class PassResult:
    """What one pass reports to the loop: enough to decide whether to idle."""

    workspaces_visited: int
    jobs_acquired: int
    deliveries_acquired: int

    @property
    def found_work(self) -> bool:
        """Whether any job **or delivery** was acquired. A busy worker does not idle.

        Both counts, because either one alone is a pass that found work: a visit that
        stopped at :data:`MAX_JOBS_PER_VISIT` deliveries with more still pending has
        exactly as much reason to loop again immediately as one that stopped at the
        cap on jobs, and reading the job count alone would put it to sleep for
        :data:`IDLE_INTERVAL_SECONDS` in between."""
        return self.jobs_acquired > 0 or self.deliveries_acquired > 0


def _validation_detail(exc: ValidationError) -> str:
    """The failing locations and messages of a stored payload, never its values."""
    parts = [
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
        for error in exc.errors(include_input=False, include_url=False)
    ]
    return "; ".join(parts)


def _finish_alone(
    engine: Engine,
    database: str,
    *,
    write: Callable[[UnitOfWork], bool],
) -> bool:
    """Run one terminal write in its own short transaction and commit it.

    The commit is unconditional because a write that matched nothing changed nothing;
    the ``bool`` comes back so the caller can decide what a zero rowcount means, which
    differs per call site.
    """
    with UnitOfWork(engine, database) as uow:
        applied = write(uow)
        uow.commit()
    return applied


def _with_operation(
    *,
    operation_id: UUID | None,
    job_write: Callable[[Connection], bool],
    operation_write: Callable[[Connection, UUID], bool],
) -> Callable[[UnitOfWork], bool]:
    """Pair a job's terminal write with its operation record's, in one transaction.

    **The rule this expresses, once, for every site that uses it.** A job whose row
    carries a non-null ``operation_id`` terminalises that ``core.operation`` record in
    the *same* transaction as its own terminal write, so the two commit together or
    neither does. A job carrying none writes nothing here and the second callable is
    never built into a statement.

    **``applied and`` is the load-bearing half.** Every job terminal write in
    ``work/jobs.py`` is predicated on this worker still holding the lease and reports
    whether it matched. A ``False`` means the row now belongs to another worker, which
    will terminalise it — and its operation record — itself. Writing the operation
    record anyway would let a worker that lost its lease record the outcome of work it
    did not finish, on a record whose job is about to end differently.

    **The operation write's own rowcount is checked, not discarded.** It is predicated
    on the record still being open, so a ``False`` means something had already
    terminalised it — two jobs enqueued against one record is the only way to reach
    that, and it is a producer defect rather than a race this worker can resolve. The
    job's own outcome stands, the record is left as whoever got there first wrote it,
    and the line is logged, because a silently dropped write is how a record ends up
    disagreeing with the job that owns it.

    A helper rather than a copy of the same few lines at every site that pairs the two
    writes: repeated inline, the one a later edit misses is the defect, and nothing
    about the shape announces which one that is. It returns the
    ``Callable[[UnitOfWork], bool]`` :func:`_finish_alone` already takes, so no call
    site changes shape beyond naming its two halves. The two terminal-write sites that
    do **not** use it — :func:`_run_handler`'s success path and
    :func:`_resolve_zero_rowcount` — each already hold an open transaction of their own
    and write the pair inline, under the same rule and with the same ``applied and``
    ordering.
    """

    def write(uow: UnitOfWork) -> bool:
        applied = job_write(uow.connection)
        if applied and operation_id is not None:
            if not operation_write(uow.connection, operation_id):
                logger.warning(
                    "operation record %s was already terminal when its job finished; "
                    "the job's own outcome stands and the record is left as it was",
                    operation_id,
                )
        return applied

    return write


def _resolve_zero_rowcount(
    engine: Engine,
    database: str,
    *,
    job_id: UUID,
    operation_id: UUID | None,
    owner: str,
    now: datetime,
) -> None:
    """The one zero-rowcount branch all four finishes fall into.

    Reached after the work transaction has already been rolled back. Re-read the row in
    a fresh short transaction: still ours, still ``leased``, ``cancel_requested`` set ->
    write ``finish_cancelled``; anything else -> the lease is genuinely lost, so log it
    and write nothing further. The re-read happens *after* the rollback so it sees
    committed truth, and if another worker acquired in between, ``finish_cancelled``'s
    own ownership predicate returns zero rows and this branch correctly does nothing.

    **This is a job-terminal-write site like the others**, and the only one whose job
    may have entered a handler or may not, depending on which caller reached it. It
    terminalises the operation record here for the same reason every other one does,
    and in the same transaction; ``operation_id`` is threaded in from the leased row
    rather than read out of the ``SELECT`` above, so every site in this module takes
    the value from one place.
    """
    cancelled = False
    with UnitOfWork(engine, database) as uow:
        row = uow.connection.execute(
            select(t.job.c.state, t.job.c.lease_owner, t.job.c.cancel_requested).where(
                t.job.c.id == job_id
            )
        ).one_or_none()
        if (
            row is not None
            and row.state == LEASED
            and row.lease_owner == owner
            and bool(row.cancel_requested)
        ):
            cancelled = finish_cancelled(
                uow.connection, job_id=job_id, owner=owner, now=now
            )
            if cancelled and operation_id is not None:
                if not operation_records.finish_cancelled(
                    uow.connection, operation_id=operation_id, now=now
                ):
                    logger.warning(
                        "operation record %s was already terminal when its job was "
                        "cancelled; the record is left as it was",
                        operation_id,
                    )
            uow.commit()
    if not cancelled:
        logger.warning(
            "job %s is no longer this worker's to finish; writing nothing", job_id
        )


def _run_leased_job(
    engine: Engine,
    database: str,
    *,
    leased: LeasedJob,
    kinds: JobKindRegistry,
    owner: str,
    now: datetime,
    clock: Callable[[], datetime],
    jitter: random.Random | None,
) -> None:
    """One leased job, from the two pre-handler gates to its terminal write.

    **The gates are ordered, and the order is the rule.** Cancellation wins over
    exhaustion: a job that is both cancelled and out of budget ends ``cancelled``.

    **Both gates terminalise the job's operation record, and that is not an oversight
    carried over from chunk 01's pre-handler exclusion.** That chunk's fresh-clock fix
    was about staleness, which is only a defect after a handler has run, so it drew a
    line at the handler and left these two gates on the ``now`` side of it. The rule
    here has no such line: a job that never enters its handler still reaches a terminal
    state, and a ``long_running`` operation whose job ends here and whose record is not
    moved sits ``pending`` for ever.
    """
    if leased.cancel_requested:
        applied = _finish_alone(
            engine,
            database,
            write=_with_operation(
                operation_id=leased.operation_id,
                job_write=lambda conn: finish_cancelled(
                    conn, job_id=leased.id, owner=owner, now=now
                ),
                operation_write=(
                    lambda conn, record_id: operation_records.finish_cancelled(
                        conn, operation_id=record_id, now=now
                    )
                ),
            ),
        )
        if not applied:
            logger.warning(
                "gate 1 could not cancel job %s, which this pass had just leased",
                leased.id,
            )
        return

    if leased.attempts > leased.max_attempts:
        # The crash-side half of the retry budget: a dead worker reports nothing, so
        # the next lease is the first moment anything can observe exhaustion. ``>``,
        # one more than the exception arm's ``>=``, deliberately — both arms grant
        # exactly ``max_attempts`` runs.
        applied = _finish_alone(
            engine,
            database,
            write=_with_operation(
                operation_id=leased.operation_id,
                job_write=lambda conn: finish_failed(
                    conn,
                    job_id=leased.id,
                    owner=owner,
                    now=now,
                    error=RETRY_BUDGET_EXHAUSTED,
                ),
                operation_write=(
                    lambda conn, record_id: operation_records.finish_failed(
                        conn,
                        operation_id=record_id,
                        now=now,
                        error_code=JOB_FAILED,
                        error_text=RETRY_BUDGET_EXHAUSTED,
                    )
                ),
            ),
        )
        if not applied:
            logger.warning(
                "gate 2 could not fail job %s, which this pass had just leased",
                leased.id,
            )
        return

    try:
        input_model, handler = kinds.lookup(leased.kind)
    except JobKindUnknown as exc:
        _fail_terminally(
            engine, database, leased=leased, owner=owner, now=now, error=str(exc)
        )
        return
    try:
        payload = input_model.model_validate(leased.payload)
    except ValidationError as exc:
        _fail_terminally(
            engine,
            database,
            leased=leased,
            owner=owner,
            now=now,
            error=(
                f"job kind {leased.kind!r} rejected its stored input: "
                f"{_validation_detail(exc)}"
            ),
        )
        return

    _run_handler(
        engine,
        database,
        leased=leased,
        handler=handler,
        payload=payload,
        owner=owner,
        clock=clock,
        jitter=jitter,
    )


def _fail_terminally(
    engine: Engine,
    database: str,
    *,
    leased: LeasedJob,
    owner: str,
    now: datetime,
    error: str,
) -> None:
    """Terminal on first sight: neither an unknown kind nor a payload that cannot parse
    can succeed on a later attempt, so neither is ever requeued."""
    applied = _finish_alone(
        engine,
        database,
        write=_with_operation(
            operation_id=leased.operation_id,
            job_write=lambda conn: finish_failed(
                conn, job_id=leased.id, owner=owner, now=now, error=error
            ),
            operation_write=lambda conn, record_id: operation_records.finish_failed(
                conn,
                operation_id=record_id,
                now=now,
                error_code=JOB_FAILED,
                error_text=error,
            ),
        ),
    )
    if not applied:
        _resolve_zero_rowcount(
            engine,
            database,
            job_id=leased.id,
            operation_id=leased.operation_id,
            owner=owner,
            now=now,
        )


def _run_handler(
    engine: Engine,
    database: str,
    *,
    leased: LeasedJob,
    handler: Callable[[HandlerUnitOfWork, BaseModel, CancellationToken], None],
    payload: BaseModel,
    owner: str,
    clock: Callable[[], datetime],
    jitter: random.Random | None,
) -> None:
    """Transaction 2: the handler's effects, the row's terminal write, and — when the
    job carries one — its ``core.operation`` record's, together.

    **Commit is the last thing that happens, and only when the finish applied.** The
    tempting wrong shape — run, finish, commit, then look at the finish's return value
    — reads the prose faithfully and duplicates every effect on lease theft: the
    loser's effects commit, then the winner runs and its effects land too, with success
    recorded once.

    **This function takes ``clock`` and no ``now``, deliberately.** Every write it makes
    happens *after* the handler returned or raised, and a handler's duration is
    unbounded, so the acquire instant is not merely stale here, it is wrong: it would
    make ``finished_at`` predate the work it closes and ``next_run_at`` land in the
    past. Each outcome reads ``clock()`` at the point it writes — once per outcome, not
    once per call, so the success arm and the failure arm each get their own reading.
    The pre-handler instant stays where it belongs: :func:`_run_leased_job`'s two gates
    and :func:`_fail_terminally`, none of which ever enters a handler.
    """
    token = CancellationToken(
        engine,
        job_id=leased.id,
        owner=owner,
        lease_seconds=LEASE_SECONDS,
        heartbeat_seconds=HEARTBEAT_SECONDS,
        clock=clock,
    )
    stopped = False
    failure: BaseException | None = None
    with UnitOfWork(engine, database) as work_uow:
        try:
            # **The one place a ``VerifiedScheduledExecution`` is minted**, and it is
            # minted here rather than in :func:`_run_leased_job` because the facts it
            # attests — the lease still held, exactly one enabled schedule of this
            # kind — have to be read in the transaction the handler then runs in, not
            # in an earlier one whose answer could already be stale. For the
            # overwhelming majority of jobs it is one small local ``SELECT`` that
            # answers ``None``; see ``verified_execution_for``'s own docstring for why
            # that query is the first of the checks and not the last.
            handler(
                HandlerUnitOfWork(
                    work_uow,
                    scheduled_execution=verified_execution_for(
                        work_uow.connection,
                        leased=leased,
                        database=database,
                        owner=owner,
                    ),
                ),
                payload,
                token,
            )
        except (JobCancelled, JobLeaseLost) as exc:
            # Caught before the generic branch below: one exception type raised
            # specifically to be caught, caught narrowly. Defence in depth rather than
            # the thing that makes the cancelled row true on its own — the three
            # finishes all carry ``AND cancel_requested = false``, so a JobCancelled
            # that fell through to the generic branch would reach the same
            # zero-rowcount re-read by another route. Do not weaken a predicate to
            # make this ordering "matter" again.
            work_uow.rollback()
            logger.info("job %s stopped before finishing: %s", leased.id, exc)
            stopped = True
        except Exception as exc:
            work_uow.rollback()
            failure = exc
        else:
            if finish_succeeded(
                work_uow.connection, job_id=leased.id, owner=owner, now=clock()
            ):
                if leased.operation_id is not None:
                    # Inside transaction 2, beside the handler's own effects and the
                    # row's ``succeeded`` write: the three commit together or none of
                    # them does. ``handler_returned`` is the terminal check, because
                    # what was checked here is exactly that — the handler returned
                    # without raising. Its own ``clock()`` reading at its own write
                    # point, for the reason this function's docstring gives: a
                    # handler's duration is unbounded, so the acquire instant is not
                    # the instant this record finished.
                    finished_at = clock()
                    if not operation_records.finish_succeeded(
                        work_uow.connection,
                        operation_id=leased.operation_id,
                        now=finished_at,
                        terminal_check_kind=operation_records.HANDLER_RETURNED,
                        terminal_check_at=finished_at,
                    ):
                        logger.warning(
                            "operation record %s was already terminal when its job "
                            "succeeded; the record is left as it was",
                            leased.operation_id,
                        )
                work_uow.commit()
                return
            work_uow.rollback()
            stopped = True

    if failure is not None:
        # Decide by the **row's** attempts, never by the setting. Then check the
        # write's own return value: with ``AND cancel_requested = false`` on both
        # ``finish_failed`` and ``requeue_for_retry``, a False from either means a
        # cancellation landed on a job whose handler then raised without ever
        # checkpointing, and the row must end ``cancelled`` rather than ``failed`` or
        # ``queued`` under a backoff of up to an hour.
        logger.warning("job %s raised: %s", leased.id, failure)
        stopped = not _write_failure_outcome(
            engine,
            database,
            leased=leased,
            owner=owner,
            clock=clock,
            jitter=jitter,
            error=str(failure),
        )
    # Unconditionally fresh, because the three paths that reach here disagree about
    # whether a post-handler instant was taken at all, and none of them leaves one where
    # this line could reuse it. The generic-exception path has just had
    # ``_write_failure_outcome`` take one, but that function computes it internally and
    # never hands it back; the ``finish_succeeded`` zero-rowcount path above took one
    # too, inline in the call expression, and never bound it to a name; the
    # ``JobCancelled``/``JobLeaseLost`` path took none. One reading here is correct on
    # all three and cheaper than branching on which one this is.
    if stopped:
        _resolve_zero_rowcount(
            engine,
            database,
            job_id=leased.id,
            operation_id=leased.operation_id,
            owner=owner,
            now=clock(),
        )


def _write_failure_outcome(
    engine: Engine,
    database: str,
    *,
    leased: LeasedJob,
    owner: str,
    clock: Callable[[], datetime],
    jitter: random.Random | None,
    error: str,
) -> bool:
    """The exception arm's own write, and whether it applied.

    **``clock``, not ``now``, and it is read exactly once here.** This runs after the
    handler raised, so the acquire instant would put ``next_run_at = now +
    backoff_for(attempts)`` in the past for any handler slower than its own backoff —
    the first step is five seconds — and the job would be re-leased immediately, back to
    back, until its budget was gone. One reading serves both branches so the instant a
    job failed at and the instant its retry is measured from cannot disagree.

    **Only the exhausted branch terminalises an operation record. The requeue branch
    deliberately does not, and here that is a decision rather than an omission.**
    ``requeue_for_retry`` writes ``state = QUEUED``: the job has not finished, it is
    going to run again, and moving its operation record to ``failed`` here would
    report an operation failed while its own job sits queued for another attempt —
    against AC 16's "when it finishes", and unrecoverable, because the record would
    then be terminal and the next attempt's success would have nowhere to land.
    """
    now = clock()
    if leased.attempts >= leased.max_attempts:
        return _finish_alone(
            engine,
            database,
            write=_with_operation(
                operation_id=leased.operation_id,
                job_write=lambda conn: finish_failed(
                    conn, job_id=leased.id, owner=owner, now=now, error=error
                ),
                operation_write=(
                    lambda conn, record_id: operation_records.finish_failed(
                        conn,
                        operation_id=record_id,
                        now=now,
                        error_code=JOB_FAILED,
                        error_text=error,
                    )
                ),
            ),
        )
    return _finish_alone(
        engine,
        database,
        write=lambda uow: requeue_for_retry(
            uow.connection,
            job_id=leased.id,
            owner=owner,
            now=now,
            next_run_at=now + backoff_for(leased.attempts, jitter=jitter),
            error=error,
        ),
    )


class _EventMissing(LookupError):
    """A leased delivery whose ``core.outbox_event`` row is not there.

    Its own type, and raised specifically to be caught narrowly, the way
    ``JobCancelled``/``JobLeaseLost`` are on the job side. It is the one raise out of
    :func:`_run_consumer`'s transaction that must **not** be requeued: ``event_id``
    carries no foreign key, so the state is reachable, and an outbox row that is not
    there now will not be there on the eighth attempt either. A bare ``LookupError``
    would be indistinguishable from one a consumer handler raised, and that handler's
    failure is genuinely retryable."""


def _envelope_for(conn: Connection, leased: LeasedDelivery) -> EventEnvelope:
    """What one consumer is handed for one attempt: the outbox row plus the delivery
    context the leased row already carries.

    ``publish`` writes the outbox row and every delivery row for it in one transaction,
    so a delivery whose event is missing is a broken invariant rather than a race —
    which is why :class:`_EventMissing` is terminal on first sight rather than
    requeued, matching :func:`_fail_terminally`'s rule on the job side for an input
    that cannot parse. The reason lands on the delivery row where an operator can read
    it, rather than ending the visit with a traceback.
    """
    row = conn.execute(
        select(t.outbox_event).where(t.outbox_event.c.id == leased.event_id)
    ).one_or_none()
    if row is None:
        raise _EventMissing(
            f"event {leased.event_id} has no outbox row, so the delivery to "
            f"{leased.consumer_id!r} cannot be built"
        )
    return EventEnvelope(
        id=row.id,
        position=int(row.position),
        type=str(row.type),
        schema_version=int(row.schema_version),
        source=str(row.source),
        subject_ref=str(row.subject_ref),
        subject_revision=int(row.subject_revision),
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
        actor_kind=str(row.actor_kind),
        actor_id=row.actor_id,
        occurred_at=row.occurred_at,
        data=row.data,
        consumer_id=leased.consumer_id,
        attempt=leased.attempts,
    )


def _lease_lost(leased: LeasedDelivery) -> None:
    """What a ``False`` from any of the three delivery finishes means, in one place.

    ``mark_delivered``, ``requeue_delivery`` and ``fail_delivery`` are each predicated
    on ``_held(event_id, consumer_id, owner)``, so ``False`` says this worker's lease
    lapsed and another worker re-leased the row. The attempt is abandoned — never
    retried in place, because the row now belongs to whoever holds it. There is no
    delivery-side equivalent of :func:`_resolve_zero_rowcount`: a delivery has no
    cancellation request to re-read, so there is nothing a second look could discover.
    """
    logger.warning(
        "the delivery of event %s to %s is no longer this worker's to finish; "
        "writing nothing further",
        leased.event_id,
        leased.consumer_id,
    )


def _fail_delivery_alone(
    engine: Engine,
    database: str,
    *,
    leased: LeasedDelivery,
    owner: str,
    clock: Callable[[], datetime],
    error: str,
) -> None:
    """Write ``failed`` in its own short transaction, for the three delivery outcomes
    that are terminal without a retry.

    Named rather than described as a shape, because the three do not share one: the
    **spent budget** (:func:`_run_leased_delivery`'s gate) is terminal because the
    budget is gone; an **unknown consumer** and a **missing outbox row** are terminal
    on first sight, because neither can succeed on a later attempt. The delivery-side
    counterpart of :func:`_finish_alone` plus :func:`_fail_terminally` together.

    ``failed`` blocks the head of that ``(consumer_id, subject_ref)`` queue until a
    person acts, which is the ratified behaviour rather than a cost of this branch.
    """
    applied = _finish_alone(
        engine,
        database,
        write=lambda uow: fail_delivery(
            uow.connection,
            event_id=leased.event_id,
            consumer_id=leased.consumer_id,
            owner=owner,
            now=clock(),
            error=error,
        ),
    )
    if not applied:
        _lease_lost(leased)


def _run_leased_delivery(
    engine: Engine,
    database: str,
    *,
    leased: LeasedDelivery,
    consumers: ConsumerRegistry,
    owner: str,
    clock: Callable[[], datetime],
    jitter: random.Random | None,
) -> None:
    """One leased delivery, from the pre-handler budget gate to its outcome write.

    **The gate is the twin of :func:`_run_leased_job`'s gate 2, and it is the only
    thing that ends a delivery whose worker keeps dying.** A crash raises nothing, so
    it never reaches :func:`_write_delivery_failure`'s check; the next lease is the
    first moment anything can observe that the budget is spent. Without this, a
    consumer that kills its process is re-leased and re-run past ``work.max_attempts``
    for ever, never ``failed``, never in the failure list, and holding the head of its
    ``(consumer_id, subject_ref)`` queue behind a row no operator can release.

    **``>``, one more than the exception arm's ``>=``, deliberately — and the
    asymmetry is the accounting, not a typo.** ``attempts`` is incremented by the
    lease, so the *n*-th attempt reads *n*. The raise arm fires at ``n >= budget``, on
    the attempt that has just run and failed, so ``budget`` runs happened. This arm
    fires on a freshly leased row *before* anything runs, so it must let ``budget``
    through and refuse ``budget + 1``. Written ``>=`` it would refuse the *n*-th
    attempt itself and grant one run fewer than the job side does for the same
    configured budget — a difference no test that watches only the terminal state can
    see, which is why ``test_event_delivery.py`` pins both sides of this boundary.

    The lookup below is the delivery-side equivalent of ``kinds.lookup``, and an
    unknown consumer ends the same way an unknown job kind does: terminal, without
    entering a handler. It runs after the gate for the same reason gate 2 runs before
    ``kinds.lookup``: a budget that is already spent is not worth resolving a
    subscription for.
    """
    if leased.attempts > resolve().get_int("work.max_attempts"):
        _fail_delivery_alone(
            engine,
            database,
            leased=leased,
            owner=owner,
            clock=clock,
            error=RETRY_BUDGET_EXHAUSTED,
        )
        return

    try:
        subscription = consumers.lookup(leased.consumer_id)
    except ConsumerUnknown as exc:
        _fail_delivery_alone(
            engine, database, leased=leased, owner=owner, clock=clock, error=str(exc)
        )
        return

    _run_consumer(
        engine,
        database,
        leased=leased,
        handler=subscription.handler,
        owner=owner,
        clock=clock,
        jitter=jitter,
    )


def _run_consumer(
    engine: Engine,
    database: str,
    *,
    leased: LeasedDelivery,
    handler: ConsumerHandler,
    owner: str,
    clock: Callable[[], datetime],
    jitter: random.Random | None,
) -> None:
    """Transaction 2 on the delivery side: the consumer's effects, the dedup ledger
    row and the delivery's ``delivered`` write, together.

    **The three writes share one transaction, and that is the whole of exactly-once.**
    Committing the handler's effects and recording the delivery separately would
    deliver twice on any crash between them; committing the ledger row before the
    handler would lose the effects of a handler that then raised while claiming it had
    run. Neither is a window a later retry closes.

    **``already_processed`` and ``record_processed`` guard different failures.** The
    check is the fast path that keeps a redelivery from re-running a handler; the
    primary key on ``(consumer_id, event_id)`` is the backstop that makes a second
    insert *raise* when the check was bypassed — by a lapsed lease leaving two workers
    in the same handler, or by an edit that deleted the check. That raise is a handler
    failure like any other here: it rolls this transaction back and takes the requeue
    or the terminal write below. It is never caught and turned into a ``delivered``.

    **Commit is the last thing that happens, and only when the finish applied**, for
    the reason :func:`_run_handler` gives on the job side: a ``False`` from
    ``mark_delivered`` means the lease lapsed and another worker re-leased the row, so
    committing first and inspecting the return value afterwards ships the loser's
    effects and lets the winner run them again.

    **This function takes ``clock`` and no ``now``.** Every write it makes happens
    after the handler returned or raised, and a handler's duration is unbounded, so
    each outcome reads ``clock()`` at its own write point. The instant the lease was
    taken on belongs to the lease.
    """
    failure: BaseException | None = None
    missing: _EventMissing | None = None
    lost = False
    with UnitOfWork(engine, database) as uow:
        connection = uow.connection
        try:
            if not already_processed(
                connection, consumer_id=leased.consumer_id, event_id=leased.event_id
            ):
                handler(HandlerUnitOfWork(uow), _envelope_for(connection, leased))
                record_processed(
                    connection,
                    consumer_id=leased.consumer_id,
                    event_id=leased.event_id,
                    now=clock(),
                )
            applied = mark_delivered(
                connection,
                event_id=leased.event_id,
                consumer_id=leased.consumer_id,
                owner=owner,
                now=clock(),
            )
        except _EventMissing as exc:
            # Caught before the generic branch: one exception type raised specifically
            # to be caught, caught narrowly, exactly as :func:`_run_handler` catches
            # ``JobCancelled``/``JobLeaseLost`` ahead of its own generic arm. This is
            # the one raise out of this block that is terminal rather than retried.
            uow.rollback()
            missing = exc
        except Exception as exc:
            uow.rollback()
            failure = exc
        else:
            if applied:
                uow.commit()
                return
            uow.rollback()
            lost = True

    if lost:
        _lease_lost(leased)
        return
    if missing is not None:
        logger.warning(
            "the delivery of event %s to %s cannot be built: %s",
            leased.event_id,
            leased.consumer_id,
            missing,
        )
        _fail_delivery_alone(
            engine,
            database,
            leased=leased,
            owner=owner,
            clock=clock,
            error=str(missing),
        )
        return
    if failure is not None:
        logger.warning(
            "the delivery of event %s to %s raised: %s",
            leased.event_id,
            leased.consumer_id,
            failure,
        )
        if not _write_delivery_failure(
            engine,
            database,
            leased=leased,
            owner=owner,
            clock=clock,
            jitter=jitter,
            error=str(failure),
        ):
            _lease_lost(leased)


def _write_delivery_failure(
    engine: Engine,
    database: str,
    *,
    leased: LeasedDelivery,
    owner: str,
    clock: Callable[[], datetime],
    jitter: random.Random | None,
    error: str,
) -> bool:
    """The delivery exception arm's own write, and whether it applied.

    **``clock``, not ``now``, and it is read exactly once here**, for the reason
    :func:`_write_failure_outcome` gives: one reading serves both branches so the
    instant a delivery failed at and the instant its retry is measured from cannot
    disagree, and neither can be the pre-handler instant.

    **The budget is the setting, read here, because ``core.event_delivery`` has no
    ``max_attempts`` column** — the job side reads the row's own value instead. The
    comparison is ``>=`` against an ``attempts`` the lease has already counted, so a
    budget of *n* grants exactly *n* runs and the attempt after the last ends
    ``failed`` rather than being requeued for ever.
    """
    now = clock()
    if leased.attempts >= resolve().get_int("work.max_attempts"):
        return _finish_alone(
            engine,
            database,
            write=lambda uow: fail_delivery(
                uow.connection,
                event_id=leased.event_id,
                consumer_id=leased.consumer_id,
                owner=owner,
                now=now,
                error=error,
            ),
        )
    return _finish_alone(
        engine,
        database,
        write=lambda uow: requeue_delivery(
            uow.connection,
            event_id=leased.event_id,
            consumer_id=leased.consumer_id,
            owner=owner,
            now=now,
            error=error,
            jitter=jitter,
        ),
    )


def _earliest(*instants: datetime | None) -> datetime | None:
    """The earliest instant present, or ``None`` when none is — Postgres ``LEAST``
    over a set that may be entirely null, in Python.

    The union :func:`visit_workspace` reports is computed here rather than in a join,
    because jobs, deliveries, and schedules live in three tables with three different
    state vocabularies and a query spanning them would be a fourth place to keep in
    step with ``jobs.earliest_due_at``, ``deliveries.earliest_delivery_due_at``, and
    ``schedules.earliest_schedule_due_at``.
    """
    present = [instant for instant in instants if instant is not None]
    return min(present) if present else None


def visit_workspace(
    workspace: DueWorkspace,
    *,
    kinds: JobKindRegistry,
    consumers: ConsumerRegistry,
    backend: PostgresBackend,
    owner: str,
    clock: Callable[[], datetime],
    jitter: random.Random | None,
) -> VisitResult:
    """Drain up to :data:`MAX_JOBS_PER_VISIT` jobs out of one workspace database, and
    then up to the same bound of event deliveries.

    **Jobs first, then deliveries, and both bounded by the same constant.** The order
    is not a priority claim — it is that a job handler may publish, so draining jobs
    first lets what they published go out in the same visit instead of waiting for the
    next one. The bound is shared because it exists to stop one workspace holding a
    worker for ever, and two separate budgets would double that hold.

    **``kinds`` and ``consumers`` are both injected, and neither has a default here or
    anywhere on the path down from :func:`worker_loop`.** The one production instance
    of each is built in the worker's composition root; a module-level registry would be
    global state two tests could not isolate from each other.

    **This function takes no ``now``, only ``clock``, calls it fresh at the top of every
    per-job and per-delivery iteration for the acquire, and calls it again at every
    write made after a handler ran.** Those are two different stalenesses with two
    different costs, and a docstring here that named only the first is what let the
    second survive review.

    *Between jobs — wasted work.* A pass can run for seconds — up to the cap, times the
    workspaces the index returned, times handler time — so one instant captured at pass
    start goes stale as the pass runs: ``lease_until = now + LEASE_SECONDS`` lags real
    wall time by however long the pass has been going, inviting a lease steal before the
    first heartbeat, and the cap branch below would report the visit due at a moment
    already past. Ownership is never threatened — every predicate holds however stale
    ``now`` is — so here the cost really is only wasted work and inflated ``attempts``.

    *Within one job — a wrong instant, not a stale one.* The acquire instant reaching a
    write made **after** the handler is worse, because the handler's own duration is
    unbounded: ``next_run_at = now + backoff_for(1)`` is ``+5 s``, so a handler that
    fails after longer than that is requeued at an instant already in the past and is
    re-leased with no delay, burning its whole budget back to back; ``finished_at`` and
    the lease release carry the same wrong instant on the path that runs most often.
    That is why :func:`_run_handler` takes ``clock`` and no ``now``.

    The same ``clock`` is what the per-attempt ``CancellationToken`` runs on, which is
    what makes a handler's checkpoints controllable from a test.

    **``backend`` is typed ``PostgresBackend``, not the ``StorageBackend`` Protocol**:
    the Protocol declares neither ``pools`` nor ``control_engine``, and this package is
    mypy-strict, so the Protocol annotation would fail the type gate the moment either
    is read. ``PostgresBackend`` is release one's only implementation and is what
    ``get_backend()`` returns.

    **This function catches nothing, on its own behalf or anyone else's** — every
    exception propagates to :func:`run_one_pass`, whose single ``try`` wraps this whole
    call. A guard placed here around the routing step alone would cover only the
    failure that is unreachable in practice (``workspaces_with_due_work`` already
    filters ``state = 'active'``, so a ``workspace_unavailable`` refusal needs a race
    between the index read and this call) and none of the ones a first run actually
    produces.

    **Schedules are a one-function insertion.** After ``pools.acquire`` and before
    the job-drain loop this function opens a short unit of work and calls
    ``run_due_schedules(conn, *, workspace_id, now)`` on that connection — the visit
    does not hold a connection at entry, only an engine. Due rows enqueue with the
    visit's ``workspace_id``. The remaining-instant write below folds the soonest
    enabled schedule instant into :func:`_earliest` beside jobs and deliveries.
    """
    row = active_workspace(workspace.workspace_id)
    database = row.database_name
    with backend.pools.acquire(database) as engine:
        now = clock()
        with UnitOfWork(engine, database) as uow:
            run_due_schedules(
                uow.connection,
                workspace_id=workspace.workspace_id,
                now=now,
            )
            uow.commit()
        acquired = 0
        drained = False
        for _ in range(MAX_JOBS_PER_VISIT):
            now = clock()
            with UnitOfWork(engine, database) as uow:
                leased = acquire_lease(
                    uow.connection, owner=owner, now=now, lease_seconds=LEASE_SECONDS
                )
                uow.commit()
            if leased is None:
                drained = True
                break
            acquired += 1
            _run_leased_job(
                engine,
                database,
                leased=leased,
                kinds=kinds,
                owner=owner,
                now=now,
                clock=clock,
                jitter=jitter,
            )

        deliveries_acquired = 0
        deliveries_drained = False
        for _ in range(MAX_JOBS_PER_VISIT):
            with UnitOfWork(engine, database) as uow:
                leased_delivery = lease_delivery(
                    uow.connection,
                    owner=owner,
                    now=clock(),
                    lease_seconds=LEASE_SECONDS,
                )
                uow.commit()
            if leased_delivery is None:
                deliveries_drained = True
                break
            deliveries_acquired += 1
            _run_leased_delivery(
                engine,
                database,
                leased=leased_delivery,
                consumers=consumers,
                owner=owner,
                clock=clock,
                jitter=jitter,
            )

        with UnitOfWork(engine, database) as uow:
            remaining = _earliest(
                earliest_due_at(uow.connection),
                earliest_delivery_due_at(uow.connection),
                earliest_schedule_due_at(uow.connection),
            )
        # A visitor that stopped at the cap with work still to do must say so in the
        # index immediately rather than let the workspace wait out the 900 s
        # reconcile floor, so the instant it reports is read *here*, once this
        # branch is known to fire — after the last handler returned. The last
        # job's acquire instant, which this line used to report, is stale by the
        # whole of that handler's run. ``remaining is None`` means the cap and
        # the drain coincided.
        next_due_at = (
            remaining
            if (drained and deliveries_drained) or remaining is None
            else clock()
        )
        return VisitResult(
            next_due_at=next_due_at,
            jobs_acquired=acquired,
            deliveries_acquired=deliveries_acquired,
        )


def run_one_pass(
    *,
    kinds: JobKindRegistry,
    consumers: ConsumerRegistry,
    backend: PostgresBackend,
    owner: str,
    now: datetime,
    clock: Callable[[], datetime],
    jitter: random.Random | None,
    reconcile_seconds: int,
) -> PassResult:
    """One sweep: read the due-work index, visit each workspace, record each visit.

    **``now`` here is the pass-level instant, the one value in this module that is
    deliberately not refreshed per job.** It drives the due-work index and nothing
    else: the read below, and the coarse back-off written for a failed visit. Everything
    that leases, finishes or requeues a job runs on ``visit_workspace``'s own per-job
    ``now``, which is why ``clock`` is passed down and ``now`` is not.

    **The control-plane transaction for the read is closed before anything is visited.**
    ``record_visit``'s compare-and-set is what lets a ``mark_work_due`` from a
    concurrent ``enqueue`` land safely mid-pass, and both share the one control engine:
    a control transaction held open for the pass's full duration would serialise every
    concurrent enqueue behind it, turning a compare-and-set into a lock queue. Each
    ``record_visit`` likewise gets its own fresh short transaction, never one shared
    across the workspaces of a pass.

    **The limit is the engine cache's own size**, so a pass never asks for more
    workspace engines than the cache can hold — the connection-budget argument made
    operational with no new settings key.
    """
    with backend.control_engine.begin() as control:
        due = workspaces_with_due_work(control, now=now, limit=backend.pools.cache_size)

    jobs_acquired = 0
    deliveries_acquired = 0
    for workspace in due:
        # One guard for the whole visit, not for the routing step inside it: every
        # failure a first run actually produces — a missing table, a poisoned engine,
        # an unexpected raise out of the job loop — lands past routing, and a narrower
        # guard would let all of them end the pass.
        try:
            visit = visit_workspace(
                workspace,
                kinds=kinds,
                consumers=consumers,
                backend=backend,
                owner=owner,
                clock=clock,
                jitter=jitter,
            )
        except Exception:
            logger.exception(
                "worker %s: visiting workspace %s failed", owner, workspace.workspace_id
            )
            # A skipped workspace still has to be recorded, or it is re-offered on the
            # very next one-second pass, for ever, with a fresh traceback each time.
            next_due_at: datetime | None = now + timedelta(seconds=LEASE_SECONDS)
        else:
            jobs_acquired += visit.jobs_acquired
            deliveries_acquired += visit.deliveries_acquired
            next_due_at = visit.next_due_at

        with backend.control_engine.begin() as control:
            record_visit(
                control,
                workspace.workspace_id,
                observed_due_at=workspace.observed_due_at,
                next_due_at=next_due_at,
                reconcile_seconds=reconcile_seconds,
            )

    backend.pools.close_idle()
    return PassResult(
        workspaces_visited=len(due),
        jobs_acquired=jobs_acquired,
        deliveries_acquired=deliveries_acquired,
    )


def _pass_backoff_seconds(consecutive_failures: int) -> float:
    """``IDLE_INTERVAL_SECONDS`` doubled per consecutive failed pass, capped."""
    doublings = min(max(consecutive_failures - 1, 0), 8)
    return min(IDLE_INTERVAL_SECONDS * float(2**doublings), MAX_PASS_BACKOFF_SECONDS)


def worker_loop(
    *,
    kinds: JobKindRegistry,
    consumers: ConsumerRegistry,
    backend: PostgresBackend | None = None,
    stop: threading.Event | None = None,
    max_passes: int | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    jitter: random.Random | None = None,
) -> None:
    """Run passes until the stop event is set or ``max_passes`` is exhausted.

    ``stop`` and ``max_passes`` exist so a test drives a bounded number of passes
    without a thread or an infinite loop; when the caller passes neither, the loop
    creates its own ``threading.Event`` so the sleep mechanism is identical either way.

    ``owner`` is generated **once**, here, and threaded through every call this loop
    makes. Never a hostname, never a pid: container pids repeat across restarts and
    replicas, and every ownership predicate in this package is exactly as good as this
    string's uniqueness.

    **This function never exits on a failure at either level** — only on ``stop`` or
    ``max_passes``. A failing workspace is handled inside :func:`run_one_pass`; a
    failing pass (the due-work read raising because ``rheo migrate`` has not run yet is
    the *first-run* path, not an edge case) is logged here and followed by a capped
    doubling from :data:`IDLE_INTERVAL_SECONDS` to :data:`MAX_PASS_BACKOFF_SECONDS`,
    reset on the first pass that succeeds.

    **Every sleep is ``stop.wait(timeout)``, never ``time.sleep``.** ``Event.wait``
    returns the instant the event is set, so a container's ``SIGTERM`` reaches this loop
    within one pass; a plain sleep would make the stop signal wait out the current
    interval, which at the 60 s ceiling is a minute of unresponsive container on every
    deploy. That is what makes the exit assertion true at the ceiling and not only at
    the floor.
    """
    active_backend = get_backend() if backend is None else backend
    stop_event = threading.Event() if stop is None else stop
    owner = f"worker-{uuid7()}"
    logger.info("worker %s starting", owner)

    passes = 0
    consecutive_failures = 0
    while not stop_event.is_set():
        if max_passes is not None and passes >= max_passes:
            break
        passes += 1
        now = clock()
        reconcile_seconds = resolve().get_int("work.due_reconcile_seconds")
        try:
            result = run_one_pass(
                kinds=kinds,
                consumers=consumers,
                backend=active_backend,
                owner=owner,
                now=now,
                clock=clock,
                jitter=jitter,
                reconcile_seconds=reconcile_seconds,
            )
        except Exception:
            consecutive_failures += 1
            delay = _pass_backoff_seconds(consecutive_failures)
            logger.exception("worker %s: pass failed; waiting %.1fs", owner, delay)
            stop_event.wait(delay)
        else:
            consecutive_failures = 0
            if not result.found_work:
                stop_event.wait(IDLE_INTERVAL_SECONDS)

    logger.info("worker %s stopping after %d pass(es)", owner, passes)
