"""Schedule ticker and the ``core.retention_sweep`` job.

``run_due_schedules`` is the one-function insertion ``loop.visit_workspace`` calls
after it acquires a workspace engine. It does not parse stored cron expressions:
release-one cadence is ``next_run_at + 1 day``. Every enqueue carries
``RetentionSweepPayload.workspace_id`` from the visit, never an empty dict.

``run_retention_sweep`` reads that id from the payload (``HandlerUnitOfWork`` has
no workspace slot), deletes expired ``core.runtime_transcript`` rows, and unlinks
regular files under that workspace's Claude CLI ``projects/`` subtree only. The
once-copied login seed at the configuration-directory root is not a session file.

**One scheduled kind is treated differently here, and only one.**
``scheduled_authority.CATCH_UP_JOB_KIND`` is the ratified exception
(``deletion-export-migration.md`` § Retention): its due row is claimed with
``SELECT ... FOR UPDATE SKIP LOCKED`` so a sweep already draining a backlog is
*observed* rather than waited for, it is not enqueued twice while one of its jobs is
still queued or leased, it is excluded from :func:`earliest_schedule_due_at` while
that is so, and :func:`rearm_catch_up_schedule` is how one committed batch asks for
the next. Every other kind — ``core.retention_sweep`` included — keeps the unlocked
once-a-day iteration it has always had, in the same pass, and that is the point of
the scoping: a blocking lock on the one row would stall all of them behind a sweep.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Connection, delete, func, literal, or_, select, update

from rheo_core.audit.tool_telemetry import (
    TOOL_RETENTION_DAYS_KEY,
    purge_expired_tool_telemetry,
)
from rheo_core.boundary.context import Refusal
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import PostgresOverrideSource
from rheo_core.storage import work_tables as t
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.runtime_tables import runtime_transcript
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import LEASED, QUEUED, enqueue_job
from rheo_core.work.scheduled_authority import CATCH_UP_JOB_KIND, sealed_execution

RETENTION_SWEEP: Final = "core.retention_sweep"
_CLI_CONFIG_DIR: Final = "claude-cli"
_SESSION_SUBDIR: Final = "projects"
_DAY: Final = timedelta(days=1)
_CATCH_UP_GAP: Final = timedelta(seconds=1)
"""How far past a batch's finish instant the next batch is asked for.

One second, and its job is to be *small but not zero*: the next visit finds the
schedule due almost immediately, and the instant is still strictly in the future when
it is written, so nothing about the row says "run me now, again, in this same pass".
"""

_UNFINISHED_JOB_STATES: Final = (QUEUED, LEASED)
"""What "a job of this kind already exists" means, for coalescing.

Both states, because both are work that will run: a ``queued`` row is a first attempt
or a retry waiting out its backoff, and a ``leased`` row is either running now or a
lease awaiting recovery after a worker died. Enqueueing a second batch beside either
would be two sweeps racing over one backlog.
"""


def _unfinished_job_exists(conn: Connection, kind: str) -> bool:
    """Whether any ``queued`` or ``leased`` ``core.job`` row carries ``kind``."""
    return bool(
        conn.execute(
            select(
                select(t.job.c.id)
                .where(t.job.c.kind == kind, t.job.c.state.in_(_UNFINISHED_JOB_STATES))
                .exists()
            )
        ).scalar_one()
    )


class RetentionSweepPayload(BaseModel):
    """The ``core.retention_sweep`` job's input. ``workspace_id`` is always present."""

    workspace_id: UUID


def earliest_schedule_due_at(conn: Connection) -> datetime | None:
    """The soonest enabled ``core.schedule.next_run_at``, or ``None``.

    Folded into ``visit_workspace``'s remaining-instant write beside job and
    delivery instants. Disabled rows are not a reason to wake the worker.

    **Nor is a coalesced catch-up schedule, and that exclusion is load-bearing.** A
    sweep that asked for another batch wrote ``finish + 1 s`` onto its own row and
    then failed, or is still leased: the row's instant is in the past and would wake
    the worker on every single pass while the job it points at sits under a retry
    backoff or a live lease. ``jobs.earliest_due_at`` already owns that wake through
    the queued retry's ``next_run_at`` or the lease's ``lease_until``, which are the
    instants something will actually happen at. Scoped to
    :data:`~rheo_core.work.scheduled_authority.CATCH_UP_JOB_KIND`: every other
    schedule's instant is reported exactly as before, coalesced or not, because no
    other kind coalesces.
    """
    coalesced = (
        select(literal(1))
        .where(
            t.job.c.kind == t.schedule.c.job_kind,
            t.job.c.state.in_(_UNFINISHED_JOB_STATES),
        )
        .exists()
    )
    value = conn.execute(
        select(func.min(t.schedule.c.next_run_at)).where(
            t.schedule.c.enabled.is_(True),
            or_(t.schedule.c.job_kind != CATCH_UP_JOB_KIND, ~coalesced),
        )
    ).scalar_one_or_none()
    return value if isinstance(value, datetime) else None


@dataclass(frozen=True, slots=True)
class ScheduleOutcome:
    """What one named schedule's row and its most recent job say about themselves.

    A12's diagnostic half: a module refusing because a sweep has not caught up needs
    to let an operator tell "it has not run yet" from "it is stuck", and those are two
    different facts on two different rows. The schedule row carries when the next run
    is due and when the last one started; the most recent job of its kind carries how
    that run ended and how much of its attempt budget is left.

    **Content-free, and deliberately short of ``last_error``.** Every field here is an
    identifier, an instant, a state name or a count. A job's ``last_error`` is an
    arbitrary exception message written by whichever handler failed, so it is not
    structurally content-free and is not published through this shape; an operator who
    needs it reads ``core.work.failures``, which is the owner/operator-only surface
    that already owns it and the surface A12 points at by name.

    Every field is nullable because a workspace may hold no such schedule at all — a
    module enabled before the schedule existed, or a schedule an operator disabled and
    removed — and "there is no row" is an answer an operator needs rather than an
    error to raise inside a refusal that is already being raised.
    """

    schedule_id: UUID | None
    enabled: bool | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    job_id: UUID | None
    job_state: str | None
    job_attempts: int | None
    job_max_attempts: int | None
    job_finished_at: datetime | None


def schedule_outcome(
    conn: Connection, *, module_id: str, name: str, job_kind: str
) -> ScheduleOutcome:
    """The named schedule's row and the newest job of its kind, on this connection.

    Read on the caller's own connection, so a refusal raised inside an export's source
    snapshot reports the schedule state **that snapshot sees**. That is the honest
    pairing: the expired rows the refusal counted and the schedule state offered as the
    remedy then describe one committed instant instead of two.

    ``(module_id, name)`` identifies the schedule and ``job_kind`` the jobs, rather
    than deriving the second from the first, because a schedule row may be absent while
    its jobs are not — exactly the "somebody removed the schedule and the backlog
    stayed" case this is meant to surface.
    """
    row = conn.execute(
        select(
            t.schedule.c.id,
            t.schedule.c.enabled,
            t.schedule.c.next_run_at,
            t.schedule.c.last_run_at,
        ).where(t.schedule.c.module_id == module_id, t.schedule.c.name == name)
    ).first()
    job = conn.execute(
        select(
            t.job.c.id,
            t.job.c.state,
            t.job.c.attempts,
            t.job.c.max_attempts,
            t.job.c.finished_at,
        )
        .where(t.job.c.kind == job_kind)
        .order_by(t.job.c.created_at.desc(), t.job.c.id.desc())
        .limit(1)
    ).first()
    return ScheduleOutcome(
        schedule_id=None if row is None else row.id,
        enabled=None if row is None else bool(row.enabled),
        next_run_at=None if row is None else row.next_run_at,
        last_run_at=None if row is None else row.last_run_at,
        job_id=None if job is None else job.id,
        job_state=None if job is None else str(job.state),
        job_attempts=None if job is None else int(job.attempts),
        job_max_attempts=None if job is None else int(job.max_attempts),
        job_finished_at=None if job is None else job.finished_at,
    )


def _claim_catch_up_row(conn: Connection, schedule_id: UUID, now: datetime) -> bool:
    """Try to claim the catch-up schedule row without waiting for it.

    ``SKIP LOCKED`` and **never** a plain blocking ``FOR UPDATE``: an in-flight sweep
    holds this row for the whole of its handler (``scheduled_authority
    ._hold_catch_up_row``), and this statement runs inside the visit's single
    pre-drain transaction, ahead of every other due schedule in the same pass. Waiting
    here would put ``core.retention_sweep`` and every other due row behind a sweep
    that may take minutes, which is the one outcome § A9 names.

    ``False`` means *skip this schedule for this visit* — no error, no enqueue, no
    ``next_run_at`` change — and it covers both ways of getting there: the row is
    locked by a sweep that already owns the finish-and-rearm decision, or it is no
    longer enabled and due (the outer read was unlocked, so it may be stale). Both
    answers are the same instruction, so they are one branch.
    """
    return (
        conn.execute(
            select(t.schedule.c.id)
            .where(
                t.schedule.c.id == schedule_id,
                t.schedule.c.enabled.is_(True),
                t.schedule.c.next_run_at <= now,
            )
            .with_for_update(skip_locked=True)
        ).first()
        is not None
    )


def run_due_schedules(conn: Connection, *, workspace_id: UUID, now: datetime) -> None:
    """Enqueue each due enabled schedule and advance it by one day. Does not commit.

    ``workspace_id`` is keyword-only beside ``now`` and is dumped onto every job
    as ``str(workspace_id)``. Stored ``cron`` is ignored. Does not enqueue ``{}``.

    **One kind takes two extra steps and the rest take none.** For
    :data:`~rheo_core.work.scheduled_authority.CATCH_UP_JOB_KIND` the row is claimed
    with :func:`_claim_catch_up_row` and then checked for an unfinished job of its own
    kind; either answer skips it for this visit, silently, and the loop carries on to
    the next due schedule in the same pass. No other kind is locked, coalesced, or
    changed in any way by this function — that is what makes a long Recallatron sweep
    invisible to ``core.retention_sweep`` rather than a queue in front of it.
    """
    budget = resolve().get_int("work.max_attempts")
    rows = conn.execute(
        select(t.schedule).where(
            t.schedule.c.enabled.is_(True),
            t.schedule.c.next_run_at <= now,
        )
    ).all()
    for row in rows:
        kind = str(row.job_kind)
        if kind == CATCH_UP_JOB_KIND and (
            not _claim_catch_up_row(conn, row.id, now)
            or _unfinished_job_exists(conn, kind)
        ):
            continue
        enqueue_job(
            conn,
            kind=kind,
            payload={"workspace_id": str(workspace_id)},
            now=now,
            max_attempts=budget,
        )
        conn.execute(
            update(t.schedule)
            .where(t.schedule.c.id == row.id)
            .values(last_run_at=now, next_run_at=now + _DAY)
        )


def rearm_catch_up_schedule(
    uow: HandlerUnitOfWork, capability: object, *, finished_at: datetime
) -> bool:
    """Move this sweep's own schedule to ``finished_at`` + one second. Does not commit.

    § A9's durable continuation marker, and the whole of it: *the schedule row that
    already exists*, moved forward by a second. No cursor, no backlog count, no second
    job state machine. It is written in the worker's one handler transaction, so it
    commits with the batch's deletions or rolls back with them — a lost lease or a
    cancellation cannot leave a continuation behind for work that did not happen.

    **Four things a caller cannot ask this for**, which is why it is a core helper and
    not an ``UPDATE`` a module writes:

    - *Another schedule.* The row is ``capability.schedule_id``, and the capability is
      re-derived from the database by
      :func:`~rheo_core.work.scheduled_authority.sealed_execution` — never read off a
      caller's argument.
    - *Another kind's schedule.* Only :data:`CATCH_UP_JOB_KIND` coalesces and only it
      rearms; a capability for any other scheduled kind answers ``False`` here.
    - *A time of its choosing.* ``finished_at`` is the caller's instant, and the only
      thing it can do with it is ask for its **own** next run one second later.
    - *A disabled schedule, enabled.* ``enabled IS TRUE`` is in the predicate, so a
      schedule an operator turned off stays off and the write matches nothing.

    Answers a ``bool`` rather than raising: the caller has just done real work, and a
    continuation that could not be written is not a reason to roll that work back. The
    daily trigger is still there, and the next tick picks the remainder up.
    """
    verified = sealed_execution(uow, capability)
    if isinstance(verified, Refusal) or verified.job_kind != CATCH_UP_JOB_KIND:
        return False
    return (
        uow.connection.execute(
            update(t.schedule)
            .where(
                t.schedule.c.id == verified.schedule_id,
                t.schedule.c.enabled.is_(True),
            )
            .values(next_run_at=finished_at + _CATCH_UP_GAP)
        ).rowcount
        == 1
    )


def run_retention_sweep(
    uow: HandlerUnitOfWork,
    payload: BaseModel,
    token: CancellationToken,
) -> None:
    """Delete expired transcripts and telemetry, and unlink expired session files.

    **The telemetry purge is this sweep's, and only this sweep's** (§ A11): the
    telemetry insert path attempts no age pruning at all, so that an ordinary MCP call
    never pays for a maintenance scan. Here it is one bounded ``DELETE`` on an indexed
    range, during the idle window the daily schedule already owns. It runs before the
    session-file walk below, which returns early on a workspace with no CLI config
    directory — a purge placed after that return would silently never run for most
    workspaces.
    """
    assert isinstance(payload, RetentionSweepPayload)
    token.checkpoint()
    now = datetime.now(UTC)
    uow.connection.execute(
        delete(runtime_transcript).where(runtime_transcript.c.retention_until < now)
    )
    settings = resolve(
        workspace_id=payload.workspace_id, source=PostgresOverrideSource()
    )
    purge_expired_tool_telemetry(
        uow.connection,
        not_before=now - timedelta(days=settings.get_int(TOOL_RETENTION_DAYS_KEY)),
    )
    retention_days = settings.get_int("runtime.transcript_retention_days")
    horizon = now - timedelta(days=retention_days)
    config_dir = (
        workspace_dir_for(payload.workspace_id, Purpose.SCRATCH) / _CLI_CONFIG_DIR
    )
    session_root = config_dir / _SESSION_SUBDIR
    if (
        not config_dir.is_dir()
        or session_root.is_symlink()
        or not session_root.is_dir()
    ):
        return
    _unlink_expired_session_files(session_root, horizon=horizon, token=token)


def _unlink_expired_session_files(
    session_root: Path,
    *,
    horizon: datetime,
    token: CancellationToken,
) -> None:
    """Unlink regular files under ``session_root`` older than ``horizon``.

    Recurses only under ``session_root``. Does not follow directory symlinks.
    Does not unlink through a file symlink, so a link whose target sits outside
    ``session_root`` is left alone. Does not delete directories.
    """
    for dirpath, dirnames, filenames in os.walk(session_root, followlinks=False):
        token.checkpoint()
        dirnames[:] = [
            name for name in dirnames if not (Path(dirpath) / name).is_symlink()
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if mtime < horizon:
                path.unlink()
