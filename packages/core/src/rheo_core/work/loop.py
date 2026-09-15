"""The worker: one process loop, one pass, one workspace visit, one job at a time.

Three functions, each the unit the one above it repeats. :func:`worker_loop` is the
process: it owns the stop event, the once-per-pass settings read and the back-off
between passes. :func:`run_one_pass` is one sweep of the due-work index: the
control-plane read, the visits, the ``record_visit`` write per workspace, and one
``close_idle`` at the end. :func:`visit_workspace` drains up to
:data:`MAX_JOBS_PER_VISIT` jobs out of one workspace database.

**The transaction map, because it is the run's biggest call.** Per job: the acquire is
its own transaction and commits immediately, because a lease that sat uncommitted for
the length of the handler would be invisible to a competing worker. The handler's
effects and the row's ``succeeded`` write share a second transaction and commit
together, so a job's effects and its success are never separable. Every non-success
outcome rolls that second transaction back and writes the terminal or requeued state in
a fresh third one. ``CancellationToken`` opens its own short transactions on top.

**The clock is injected, and there are two of them by design.**
:func:`run_one_pass` holds one pass-level ``now`` that drives the due-work index only;
:func:`visit_workspace` takes no ``now`` at all and calls ``clock()`` fresh at the top
of every per-job iteration. A pass can run for seconds, and a single instant captured
at pass start would be stale by the time it were written as a lease expiry or a retry's
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
from sqlalchemy import Engine, select

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
"""Pre-handler gate 2's error text. Fixed, because it is the only thing that
distinguishes the crash arm's ``failed`` from every other route to ``failed``."""

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
    entering a handler, because those are work the pass did.
    """

    next_due_at: datetime | None
    jobs_acquired: int


@dataclass(frozen=True, slots=True)
class PassResult:
    """What one pass reports to the loop: enough to decide whether to idle."""

    workspaces_visited: int
    jobs_acquired: int

    @property
    def found_work(self) -> bool:
        """Whether any job was actually acquired. A busy worker does not idle."""
        return self.jobs_acquired > 0


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


def _resolve_zero_rowcount(
    engine: Engine, database: str, *, job_id: UUID, owner: str, now: datetime
) -> None:
    """The one zero-rowcount branch all four finishes fall into.

    Reached after the work transaction has already been rolled back. Re-read the row in
    a fresh short transaction: still ours, still ``leased``, ``cancel_requested`` set ->
    write ``finish_cancelled``; anything else -> the lease is genuinely lost, so log it
    and write nothing further. The re-read happens *after* the rollback so it sees
    committed truth, and if another worker acquired in between, ``finish_cancelled``'s
    own ownership predicate returns zero rows and this branch correctly does nothing.
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
    """
    if leased.cancel_requested:
        applied = _finish_alone(
            engine,
            database,
            write=lambda uow: finish_cancelled(
                uow.connection, job_id=leased.id, owner=owner, now=now
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
            write=lambda uow: finish_failed(
                uow.connection,
                job_id=leased.id,
                owner=owner,
                now=now,
                error=RETRY_BUDGET_EXHAUSTED,
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
        now=now,
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
        write=lambda uow: finish_failed(
            uow.connection, job_id=leased.id, owner=owner, now=now, error=error
        ),
    )
    if not applied:
        _resolve_zero_rowcount(engine, database, job_id=leased.id, owner=owner, now=now)


def _run_handler(
    engine: Engine,
    database: str,
    *,
    leased: LeasedJob,
    handler: Callable[[HandlerUnitOfWork, BaseModel, CancellationToken], None],
    payload: BaseModel,
    owner: str,
    now: datetime,
    clock: Callable[[], datetime],
    jitter: random.Random | None,
) -> None:
    """Transaction 2: the handler's effects and the row's terminal write, together.

    **Commit is the last thing that happens, and only when the finish applied.** The
    tempting wrong shape — run, finish, commit, then look at the finish's return value
    — reads the prose faithfully and duplicates every effect on lease theft: the
    loser's effects commit, then the winner runs and its effects land too, with success
    recorded once.
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
            handler(HandlerUnitOfWork(work_uow), payload, token)
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
                work_uow.connection, job_id=leased.id, owner=owner, now=now
            ):
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
            now=now,
            jitter=jitter,
            error=str(failure),
        )
    if stopped:
        _resolve_zero_rowcount(engine, database, job_id=leased.id, owner=owner, now=now)


def _write_failure_outcome(
    engine: Engine,
    database: str,
    *,
    leased: LeasedJob,
    owner: str,
    now: datetime,
    jitter: random.Random | None,
    error: str,
) -> bool:
    """The exception arm's own write, and whether it applied."""
    if leased.attempts >= leased.max_attempts:
        return _finish_alone(
            engine,
            database,
            write=lambda uow: finish_failed(
                uow.connection, job_id=leased.id, owner=owner, now=now, error=error
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


def visit_workspace(
    workspace: DueWorkspace,
    *,
    kinds: JobKindRegistry,
    backend: PostgresBackend,
    owner: str,
    clock: Callable[[], datetime],
    jitter: random.Random | None,
) -> VisitResult:
    """Drain up to :data:`MAX_JOBS_PER_VISIT` jobs out of one workspace database.

    **This function takes no ``now``, only ``clock``, and calls it fresh at the top of
    every per-job iteration.** A pass can run for seconds — up to the cap, times the
    workspaces the index returned, times handler time — so a single instant captured at
    pass start would go stale as the pass ran: ``next_run_at = now + backoff_for(1)`` is
    ``+5 s``, already in the past on any pass older than five seconds, and
    ``lease_until = now + LEASE_SECONDS`` would lag real wall time by however long the
    pass had been running, inviting a lease steal before the first heartbeat. Neither
    threatens correctness — every ownership predicate holds no matter how stale ``now``
    is — the cost is wasted work and inflated ``attempts``. The same ``clock`` is what
    the per-attempt ``CancellationToken`` runs on, which is what makes a handler's
    checkpoints controllable from a test.

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

    **Schedules stay a one-function insertion, and this is where the run that builds
    them will look.** That run adds ``run_due_schedules(conn, *, now)`` at the top of
    this function plus one enqueue per due row — no redesign, because the visit already
    holds the workspace connection and already computes ``next_due_at`` below.
    """
    row = active_workspace(workspace.workspace_id)
    database = row.database_name
    engine = backend.pools.engine_for(database)
    acquired = 0
    drained = False
    # Bound before the loop so the capped branch below always has an instant to use.
    now = clock()
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

    with UnitOfWork(engine, database) as uow:
        remaining = earliest_due_at(uow.connection)
    # A visitor that stopped at the cap with work still to do must say so in the index
    # immediately rather than let the workspace wait out the 900 s reconcile floor, and
    # the freshest per-job ``now`` is the closest instant on hand to the moment it
    # actually stopped. ``remaining is None`` means the cap and the drain coincided.
    next_due_at = remaining if drained or remaining is None else now
    return VisitResult(next_due_at=next_due_at, jobs_acquired=acquired)


def run_one_pass(
    *,
    kinds: JobKindRegistry,
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
    for workspace in due:
        # One guard for the whole visit, not for the routing step inside it: every
        # failure a first run actually produces — a missing table, a poisoned engine,
        # an unexpected raise out of the job loop — lands past routing, and a narrower
        # guard would let all of them end the pass.
        try:
            visit = visit_workspace(
                workspace,
                kinds=kinds,
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
    return PassResult(workspaces_visited=len(due), jobs_acquired=jobs_acquired)


def _pass_backoff_seconds(consecutive_failures: int) -> float:
    """``IDLE_INTERVAL_SECONDS`` doubled per consecutive failed pass, capped."""
    doublings = min(max(consecutive_failures - 1, 0), 8)
    return min(IDLE_INTERVAL_SECONDS * float(2**doublings), MAX_PASS_BACKOFF_SECONDS)


def worker_loop(
    *,
    kinds: JobKindRegistry,
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
