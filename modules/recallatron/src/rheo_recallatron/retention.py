"""The retention sweep: one leased batch of expired memories, and how it asks for the
next one.

Architecture § A9, and the shape is deliberately small. A batch selects at most
:data:`SWEEP_BATCH_LIMIT` expired roots, hands each one to the core's verified expiry
entry, and then asks one question at the end — *is there more?* — whose only answer is
a second's nudge to this module's own schedule row. There is no cursor, no backlog
count, no second job state machine and no scheduler: the schedule row **is** the
continuation marker, and the deletions themselves are the durable progress.

**Usually it does none of that.** Recallatron never auto-forgets a memory by age, so
``recallatron.retention.expire_by_age`` is off unless a workspace turned it on, and an
inert run is this handler's *normal* behaviour rather than its edge case: the gate is
read first — before a root is selected, a window resolved or the lifecycle lock taken
— and a gated-off invocation returns an ordinary success on the ordinary daily
``next_run_at``. That is deliberately **not** the no-progress failure below: a gated-off
sweep has no remainder, and classing it as a failure would burn one retry budget and
one core failure entry per workspace per day for every workspace that simply does not
want expiry. The schedule row exists in both states, written at enable, so turning the
gate on is one settings write and needs no backfill.

**Three properties are worth more than the code that implements them.**

*The batch is one transaction and the continuation commits inside it.* The worker's
success predicate commits the deletions and the rearm together or rolls both back, so
a lost lease or a cancellation can never leave a "come back in a second" behind for
work that did not happen.

*No progress is a failure, not a success with a continuation.* A batch that found a
remainder and removed nothing raises, which spends one attempt of the ordinary job
budget and puts the job under ordinary backoff. Rearming instead would mint an empty
success every second for ever, and the only thing standing between those two
behaviours is the ``removed`` count this handler keeps.

*The policy is read once per phase.* ``eligibility.begin_request`` is the single
retention read in this module and, while the gate is on, it refuses rather than
defaulting — so a workspace whose window row went missing expires nothing at all
instead of quietly expiring against 365 days.

**What this handler does not authorise.** It holds nothing: the authority is the
sealed scheduled execution the worker minted onto its own unit of work, which the core
re-derives against the live transaction before every single deletion and again before
the rearm. This file reads that value off the view and hands it straight back.
"""

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from rheo_contracts import RecordRef, WorkspaceContext
from rheo_core.boundary import context_for_memory_expiry
from rheo_core.boundary.context import Refusal
from rheo_core.deletion import lock_workspace_lifecycle
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.operations.refusals import OperationRefused
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.scheduled_authority import ExpiredRecord, dispatch_memory_expiry_in
from rheo_core.work.schedules import rearm_catch_up_schedule

from rheo_recallatron.configuration import (
    MEMORY_RECORD_TYPE,
    MODULE_ID,
    RETENTION_DAYS_KEY,
    RETENTION_EXPIRE_BY_AGE_KEY,
)
from rheo_recallatron.eligibility import (
    begin_request,
    expire_by_age_enabled,
    memory_reference,
)
from rheo_recallatron.references import canonical_ref
from rheo_recallatron.refusals import RETENTION_UNAVAILABLE
from rheo_recallatron.storage.repository import (
    expired_memory_exists,
    list_expired_memory_roots,
)

MEMORY_RETENTION_SWEEP: Final = f"{MODULE_ID}.retention_sweep"
"""This module's own scheduled kind, separate from the core's transcript sweep.

Two sweeps, two schedules, two job kinds: they retain different things on different
policies, and one row that enqueued both would make disabling either impossible.
"""

SWEEP_SCHEDULE_NAME: Final = "memory_retention_sweep"
"""The schedule row's name, as the manifest declares it.

Named here rather than spelled in the manifest because a second reader appeared: the
export refusal reads this schedule's own state so an operator can tell "the sweep has
not run yet" from "the sweep is stuck" (§ A12). Two literals would be two things to
keep in step, and a diagnostic that silently read no row would look exactly like a
workspace with no schedule at all.
"""

MEMORY_RECORD_QUALIFIED: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}"
"""The qualified type the sweep declares it expires, assembled from its two parts.

The core's expiry entry compares this against every reference it is handed, so a sweep
authorised for memories cannot expire a record of some other type whose owner happens
to authorise it.
"""

SWEEP_BATCH_LIMIT: Final = 100
"""§ A9's bound: at most a hundred **roots** selected per leased invocation.

It bounds the selection and not the cascade. A root's ordinary dependent closure stays
indivisible — AC 7 — so a single root with a thousand dependants is still one
transaction, and the hundred is what keeps the number of *independent* closures in one
transaction finite rather than the number of rows.
"""

SWEEP_CRON: Final = "0 3 * * *"
"""Daily, early. Recorded rather than parsed: release one's ticker advances a due row
by exactly one day and reads no cron field, so this is the declared intent and the
value a later scheduler would honour."""

SWEEP_MAX_ATTEMPTS: Final = 8
"""The package default, stated rather than inherited.

§ A9 leans on ordinary retry and backoff for a no-progress batch, so the attempt
budget is part of this sweep's behaviour and not an incidental default: with it, a
workspace whose closure cannot complete gives up and appears in the failure list
instead of retrying for ever.
"""

SWEEP_AUTHORITY_MISSING: Final = "sweep_authority_missing"
"""The sweep ran with no verified scheduled execution on its unit of work.

Raised rather than returned, and it fails the job: something enqueued this kind
outside the ticker, or the schedule was disabled, removed or duplicated between the
enqueue and the lease. Either way there is no authority to expire anything with, and
finishing green would report a sweep that swept nothing as a sweep that found nothing.
"""

SWEEP_NO_PROGRESS: Final = "sweep_no_progress"
"""Expired memories remain and this batch removed none of them.

Fails the attempt on purpose (§ A9). The alternative — rearming anyway — is a
continuation that arrives a second later, finds the same rows, removes none of them
again, and does that for ever at one job per second. Ordinary retry, backoff and
``max_attempts`` apply unchanged, and exhaustion is visible in the core failure list.
"""


class MemoryRetentionSweepPayload(BaseModel):
    """What the ticker enqueues: the workspace it visited, and nothing else.

    ``extra = "forbid"`` because the ticker writes exactly ``{"workspace_id": ...}``
    and a payload carrying anything more did not come from it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID


def run_memory_retention_sweep(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """One leased batch: gate, select, expire, recheck, and rearm if there is more.

    **The gate is read first**, before a root is selected, a window resolved or the
    lifecycle lock taken (§ A9). A workspace that never turned age-based expiry on has
    no horizon at all, so there is nothing to lock against and nothing to decide: the
    handler returns, the job finishes green on the daily instant the ticker already
    wrote, and no deletion record, rearm, retry or failure entry exists. This is the
    common case, not a degenerate one.

    Past that, the order is § A9's lock order and is not rearrangeable. The schedule
    row is already held — the worker took it when it minted the capability, before
    this handler was entered — then the workspace lifecycle lock, then the retention
    setting, then each target. Reading the window before taking the lifecycle lock
    would let a writer that committed in between change which rows were expired
    underneath the batch that had already chosen them.
    """
    assert isinstance(payload, MemoryRetentionSweepPayload)
    token.checkpoint()
    capability = uow.scheduled_execution
    if capability is None:
        raise OperationRefused(
            SWEEP_AUTHORITY_MISSING,
            "this sweep carries no verified scheduled execution",
        )
    ctx = context_for_memory_expiry(payload.workspace_id)
    if isinstance(ctx, Refusal):
        raise OperationRefused(ctx.state, str(ctx))
    if not expire_by_age_enabled(ctx, uow):
        # An ordinary success with no work in it. Returning here rather than falling
        # through an unbounded window matters twice over: the window row is not read,
        # so a missing or corrupt one cannot refuse in the one state that never needed
        # it, and ``_finish`` is not reached, so nothing can rearm.
        return
    lock_workspace_lifecycle(uow.connection)
    request = begin_request(ctx, uow)
    if request is None:
        raise OperationRefused(
            RETENTION_UNAVAILABLE,
            "this workspace states no usable retention policy",
        )
    horizon = request.horizon
    days = request.retention.days
    if horizon is None or days is None:
        # The gate went off between the read above and the lifecycle lock. Same
        # answer as reading it off: no horizon, no roots, no remainder, no rearm.
        # (The two are one condition — a policy has a window or it has neither — and
        # both are named so the narrowing is the type checker's rather than a
        # comment's.)
        return
    roots = list_expired_memory_roots(
        uow.connection, horizon=horizon, limit=SWEEP_BATCH_LIMIT
    )
    removed = 0
    for root in roots:
        token.checkpoint()
        answer = dispatch_memory_expiry_in(
            uow,
            capability,
            record_type=MEMORY_RECORD_QUALIFIED,
            target_ref=_memory_target(root.id),
            gate_key=RETENTION_EXPIRE_BY_AGE_KEY,
            retention_key=RETENTION_DAYS_KEY,
            retention_days=days,
            retained_successor_ref=(
                None
                if root.retained_successor_id is None
                else memory_reference(root.retained_successor_id)
            ),
        )
        # A refusal is a skip, never a failure: an absent root (an earlier root in
        # this same batch already took it as a dependant) and one that is no longer
        # expired both land here, and § A9 asks for neither a deletion record nor a
        # count for either. Only a real removal moves the counter the progress guard
        # below reads.
        if isinstance(answer, ExpiredRecord):
            removed += 1
    _finish(uow, capability, ctx=ctx, removed=removed)


def _finish(
    uow: HandlerUnitOfWork,
    capability: object,
    *,
    ctx: WorkspaceContext,
    removed: int,
) -> None:
    """The batch-end recheck, and the one continuation it can ask for.

    **A fresh instant and a fresh policy.** Both are re-read here rather than reused
    from the top of the batch: a hundred closures take real time, and a remainder
    computed against the instant the batch *started* would count rows that have since
    fallen inside the window as still expired. The retention value is re-read through
    the same single reader for the same reason — an operator who lengthened the window
    mid-batch has changed the answer, and the honest answer is the current one.

    **A gate turned off mid-batch leaves no remainder and no rearm** (§ A9). It
    reaches here as an unbounded policy with no horizon, and a workspace with no
    horizon has nothing left to expire by definition — so the daily instant stands and
    the batch's own deletions, which were authorized when the gate was still on,
    commit as they are.
    """
    finished_at = datetime.now(UTC)
    request = begin_request(ctx, uow, now=finished_at)
    if request is None:
        raise OperationRefused(
            RETENTION_UNAVAILABLE,
            "this workspace states no usable retention policy",
        )
    horizon = request.horizon
    if horizon is None or not expired_memory_exists(uow.connection, horizon=horizon):
        # Nothing left. The daily instant the ticker wrote when it enqueued this job
        # stands, untouched: a sweep that finished its work has no reason to ask for
        # another batch, and § A9 says so in as many words.
        return
    if removed == 0:
        raise OperationRefused(
            SWEEP_NO_PROGRESS,
            "expired memories remain and this batch removed none of them",
        )
    rearm_catch_up_schedule(uow, capability, finished_at=finished_at)


def _memory_target(memory_id: UUID) -> RecordRef:
    """One memory id, as the typed reference the core's expiry entry takes.

    Through this module's own two helpers rather than by building a ``RecordRef``
    here: ``memory_reference`` is where the canonical string is spelled and
    ``canonical_ref`` is where one is parsed, and a third spelling in this file would
    be a third place the module's own reference grammar lives.
    """
    return canonical_ref(memory_reference(memory_id))
