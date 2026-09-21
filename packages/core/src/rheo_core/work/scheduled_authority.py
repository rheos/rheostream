"""The verified scheduled-execution capability, and the one deletion it authorises.

Release one's record deletion is destructive and per-action approved: a person
confirms each one, and ``core.deletion_record.approval_id`` is the row that says so.
**Scheduled retention expiry is the single exception** — nobody is there to confirm the
expiry of a superseded record at three in the morning — so something else has to stand
where the approval stands, and it has to be something a caller cannot hand itself.

That something is :class:`VerifiedScheduledExecution`. It is minted in exactly one
place, ``work/loop.py``'s leased-job path, against the row that path has just leased,
and it carries the five facts that together mean "this really is the workspace's own
scheduled work, running now": the workspace and the database it routes to, the held
lease, the job kind, the enabled owning module, and that **exactly one** enabled
schedule of that kind exists for the workspace.

**Carrying it is not the same as trusting it, and the difference is the design.** The
value is an ordinary frozen dataclass — anything in this process could build one, and
no guard on ``__init__`` would change that, since the guard's key would be readable by
whatever could call it. So :func:`sealed_execution` does not trust the object it is
handed: it refuses anything that is not the very capability the view it was given
already carries, and then re-derives every fact on that value against the live
transaction before anything is deleted or rearmed. A forged capability buys nothing,
because what is checked is the database's answer and not the object's.

``dispatch()`` gains no parameter for this, ever. A keyword on the public dispatcher
would be exactly the channel a caller could route an approval bypass through; the only
producer is the worker, and the only consumers are the two entries below.

**The expiry publishes ``core.record.deleted``, exactly as the approved coordinator
does.** ``work/loop.py`` now threads the worker's composition-root ``ConsumerRegistry``
onto the handler's view, so the event fires on this path and on the approved one
alike; a worker wired with no registry refuses ``consumers_missing`` and takes the
deletion with it rather than committing a silent removal.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from rheo_contracts import RecordRef, is_reserved_module
from sqlalchemy import Connection, select, text

from rheo_core.boundary.context import Refusal
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from rheo_core.storage import work_tables as t
from rheo_core.storage.backend import HandlerUnitOfWork, StorageRefusal
from rheo_core.storage.repositories import list_module_states
from rheo_core.storage.routing import active_workspace
from rheo_core.work.jobs import LEASED, LeasedJob

CATCH_UP_JOB_KIND: Final = "recallatron.retention_sweep"
"""The one scheduled kind whose ticker row is locked, coalesced and rearmed.

**A module's kind, named in core, and that is ratified rather than convenient**
(``deletion-export-migration.md`` § Retention, the catch-up paragraph): the drain-a
-backlog-across-batches protocol is scoped to *this exact schedule* and every other
kind — ``core.retention_sweep`` included — keeps the unlocked, once-a-day iteration it
has today. A flag on ``Schedule`` would offer the behaviour to any module that set it,
which is a wider promise than the one that was ratified and a wider blast radius than
one workspace's memory retention.

It lives in this module rather than in ``work/schedules.py`` so that the schedule
ticker can import it beside :func:`sealed_execution` without this module having to
import the ticker back.

Core still names **no module's record type and no module's settings key**:
:func:`dispatch_memory_expiry_in` takes both as parameters, for the reason that
function's own docstring gives.
"""

SCHEDULED_AUTHORITY_INVALID: Final = "scheduled_authority_invalid"
"""The capability is not this view's own, or its facts no longer hold.

One state for both, deliberately. Telling a caller *which* fact failed would describe
the workspace's schedule table to something that has already proved it is not the
worker, and the answer to every case is the same: this call is not a verified
scheduled execution, so it does not get the approval bypass.
"""

EXPIRY_TYPE_MISMATCH: Final = "expiry_type_mismatch"
"""The target reference is not of the record type this sweep declared it expires.

A sweep names one owned type and then hands over references one at a time; without the
comparison, a sweep authorised for one type could expire a record of another whose
owner happens to authorise it.
"""

RETENTION_SETTING_CHANGED: Final = "retention_setting_changed"
"""The locked re-read of the retention key disagrees with the value the sweep used.

A sweep selects its candidates against a retention window, and then deletes them one
at a time. An operator who shortens or lengthens that window in between has changed
which rows were expired, and the rows already chosen are no longer the answer to the
question that is now being asked. The re-read happens on the caller's own connection
*after* the workspace lifecycle lock is taken, which is what makes it a recheck rather
than a second copy of the same stale value.
"""

_MODULE_ENABLED: Final = "enabled"
"""The ``core.module_state.state`` value that makes a module enabled.

The same literal ``boundary/factories.py`` reads for ``enabled_modules``; both are
values of ``rheo_core.storage.core_tables.MODULE_STATES``.
"""


@dataclass(frozen=True, slots=True)
class VerifiedScheduledExecution:
    """What the worker verified about one leased job before its handler ran.

    Every field is a fact re-derivable from the workspace's own rows, and
    :func:`sealed_execution` re-derives all of them before either consumer acts.
    Nothing here is a decision, a permission or a payload: a capability says what
    *was true*, and the entry that consumes it decides what that allows.
    """

    workspace_id: UUID
    database: str
    job_id: UUID
    job_kind: str
    lease_owner: str
    module_id: str
    schedule_id: UUID

    def __post_init__(self) -> None:
        for name in ("workspace_id", "job_id", "schedule_id"):
            if not isinstance(getattr(self, name), UUID):
                raise TypeError(f"VerifiedScheduledExecution.{name} is a UUID")
        for name in ("database", "job_kind", "lease_owner", "module_id"):
            if not getattr(self, name):
                raise ValueError(
                    f"VerifiedScheduledExecution.{name} is a non-empty string"
                )


@dataclass(frozen=True, slots=True)
class ExpiredRecord:
    """What a verified expiry answers with: the ledger row's reference, and nothing
    else.

    The same disclosure rule ``core.record.delete``'s own ``RecordDeleted`` carries
    (``docs/architecture/deletion-export-migration.md``): no count, no participant
    list. The exact counters are on the content-free ledger row.
    """

    deletion_ref: str


def _single_enabled_schedule(
    conn: Connection, job_kind: str
) -> tuple[UUID, str] | None:
    """The one enabled ``core.schedule`` row for ``job_kind``, or ``None``.

    ``None`` for zero rows **and for more than one**: two enabled schedules of one
    kind mean two independent tickers enqueueing the same work, and a capability that
    named one of them would be asserting a uniqueness the table does not have.
    """
    rows = conn.execute(
        select(t.schedule.c.id, t.schedule.c.module_id).where(
            t.schedule.c.enabled.is_(True),
            t.schedule.c.job_kind == job_kind,
        )
    ).all()
    if len(rows) != 1:
        return None
    return rows[0].id, str(rows[0].module_id)


def _hold_catch_up_row(conn: Connection, schedule_id: UUID, job_kind: str) -> bool:
    """Lock the catch-up schedule row for the rest of this transaction.

    § A9's lock order begins here: *the exact schedule row before the lifecycle, the
    setting and the target*. Holding it for the length of the handler is what makes a
    long sweep visible to every other ticker — ``run_due_schedules`` attempts the same
    row with ``SKIP LOCKED`` and moves on rather than enqueueing a second batch on top
    of this one — and it is why that attempt must never be a blocking ``FOR UPDATE``.

    A **blocking** lock is right on *this* side: the ticker holds the row only for the
    few statements of its own short transaction, so a sweep that waits for it waits
    for almost nothing, and a sweep that skipped it would run with no claim on the row
    it is about to rearm.

    Scoped to :data:`CATCH_UP_JOB_KIND` by the caller. Every other scheduled kind
    reaches :func:`verified_execution_for` without locking anything, which is what
    keeps this a Recallatron-shaped change rather than a change to every schedule.

    The re-derivation after the lock is not ceremony: the read that found the row ran
    unlocked, so between it and the lock another transaction could have disabled this
    schedule, deleted it, or added a second enabled one of the same kind. § A9 denies
    authority for all three, and the only place they can be ruled out is after the
    lock is held.
    """
    locked = conn.execute(
        select(t.schedule.c.id)
        .where(t.schedule.c.id == schedule_id, t.schedule.c.enabled.is_(True))
        .with_for_update()
    ).first()
    if locked is None:
        return False
    entry = _single_enabled_schedule(conn, job_kind)
    return entry is not None and entry[0] == schedule_id


def _module_is_enabled(conn: Connection, module_id: str) -> bool:
    """Whether ``module_id`` is enabled in this workspace.

    ``core`` needs no ``core.module_state`` row, exactly as it needs none in the event
    fan-out, the resolver and the deletion participant gate.
    """
    if is_reserved_module(module_id):
        return True
    return any(
        state.module_id == module_id and state.state == _MODULE_ENABLED
        for state in list_module_states(conn)
    )


def _lease_is_held(conn: Connection, *, job_id: UUID, owner: str) -> bool:
    """Whether ``job_id`` is still ``leased`` by ``owner``."""
    return (
        conn.execute(
            select(t.job.c.id).where(
                t.job.c.id == job_id,
                t.job.c.state == LEASED,
                t.job.c.lease_owner == owner,
            )
        ).first()
        is not None
    )


def verified_execution_for(
    conn: Connection,
    *,
    leased: LeasedJob,
    database: str,
    owner: str,
) -> VerifiedScheduledExecution | None:
    """Mint a capability for ``leased``, or answer ``None``.

    **``None``, never an exception**, for every one of the ways this can fail —
    a payload with no readable workspace id, a workspace that does not route to this
    connection's database, a missing, disabled or duplicated schedule, a disabled
    owning module, a lease this worker no longer holds. The overwhelmingly common case
    is a job that is simply not scheduled work, and a raise would turn that into a
    terminal job failure; the handler is meant to receive ``None`` and get on with
    whatever it does that is not a retention expiry.

    **The schedule read comes first because it is the cheap one.** It is a single
    query against a table with a handful of rows in it, local to the transaction the
    caller already has open, and it answers ``None`` for every ordinary job before
    anything reaches the control plane. The workspace-routing check below it is a
    second connection's round trip, and only a job whose kind really is on a schedule
    pays for it.

    The workspace id comes off the payload — ``run_due_schedules`` writes exactly
    ``{"workspace_id": ...}`` onto every job it enqueues — and is then *checked*
    against the database this connection is on, which is what keeps "from a payload"
    from being the same thing as "on a payload's word".

    **The last check is a lock, and only for :data:`CATCH_UP_JOB_KIND`.** That kind's
    schedule row is held for the rest of the handler's transaction
    (:func:`_hold_catch_up_row`), which is what the ticker's ``SKIP LOCKED`` attempt
    observes. Every other kind reaches here locking nothing at all, exactly as before.
    """
    entry = _single_enabled_schedule(conn, leased.kind)
    if entry is None:
        return None
    schedule_id, module_id = entry
    claimed = leased.payload.get("workspace_id")
    try:
        workspace_id = UUID(str(claimed))
    except (TypeError, ValueError):
        return None
    try:
        row = active_workspace(workspace_id)
    except StorageRefusal:
        return None
    if row.database_name != database:
        return None
    if not _module_is_enabled(conn, module_id):
        return None
    if not _lease_is_held(conn, job_id=leased.id, owner=owner):
        return None
    if leased.kind == CATCH_UP_JOB_KIND and not _hold_catch_up_row(
        conn, schedule_id, leased.kind
    ):
        return None
    return VerifiedScheduledExecution(
        workspace_id=workspace_id,
        database=database,
        job_id=leased.id,
        job_kind=leased.kind,
        lease_owner=owner,
        module_id=module_id,
        schedule_id=schedule_id,
    )


def _still_verified(conn: Connection, capability: VerifiedScheduledExecution) -> bool:
    """Re-derive every fact the capability carries, on the caller's own connection.

    This, and not the type of the value, is what makes the capability worth anything:
    a forged one asserts facts the database does not agree with, and this function is
    where the database is asked.
    """
    on = conn.execute(text("SELECT current_database()")).scalar_one()
    if str(on) != capability.database:
        return False
    entry = _single_enabled_schedule(conn, capability.job_kind)
    if entry is None:
        return False
    schedule_id, module_id = entry
    if schedule_id != capability.schedule_id or module_id != capability.module_id:
        return False
    if not _module_is_enabled(conn, module_id):
        return False
    return _lease_is_held(conn, job_id=capability.job_id, owner=capability.lease_owner)


def sealed_execution(
    uow: HandlerUnitOfWork, capability: object
) -> VerifiedScheduledExecution | Refusal:
    """``capability`` if it is this view's own and its facts still hold; else a refusal.

    The one verification, used by both consumers of the authority — the expiry entry
    below and ``work/schedules.py``'s rearm helper — so "is this really the worker's
    own scheduled execution?" is answered in one place and cannot come to differ
    between the deletion and the continuation it commits with.

    Three questions, in this order and for three different reasons. Is it the right
    *type*; is it the very object the worker minted onto **this** view (identity, not
    equality: an equal copy would mean a handler could assemble the facts itself,
    which is the whole thing this value exists to prevent); and do the facts it
    asserts still match the database (:func:`_still_verified`).
    """
    if not isinstance(capability, VerifiedScheduledExecution):
        return Refusal(
            SCHEDULED_AUTHORITY_INVALID,
            "a verified scheduled execution is required here",
        )
    if capability is not uow.scheduled_execution:
        return Refusal(
            SCHEDULED_AUTHORITY_INVALID,
            "the capability is not the one this unit of work carries",
        )
    if not _still_verified(uow.connection, capability):
        return Refusal(
            SCHEDULED_AUTHORITY_INVALID,
            "this is no longer a verified scheduled execution",
        )
    return capability


def dispatch_memory_expiry_in(
    uow: HandlerUnitOfWork,
    capability: object,
    *,
    record_type: str,
    target_ref: RecordRef,
    retention_key: str,
    retention_days: int,
    retained_successor_ref: str | None = None,
) -> ExpiredRecord | Refusal:
    """Expire one owned record under a verified schedule, with no approval.

    The one deletion path in the tree that mints no ``core.approval`` row. Everything
    else about it is the approved coordinator's shape: the owner's own content-free
    authorizer decides, every enabled participant runs, one content-free ledger row
    records the outcome, and all of it commits with the caller's transaction or none
    of it does.

    ``record_type`` is the qualified ``<module>.<record_type>`` the sweep declared it
    expires and ``target_ref`` is the one record; they are compared rather than
    assumed, because a sweep hands over references one at a time and an owner that
    authorises a reference of another type would otherwise be enough.

    ``retention_key`` and ``retention_days`` are the sweep's own authority, rechecked:
    the key is read back through :class:`~rheo_core.settings.storage_source.
    TransactionBoundOverrideSource` on this transaction's connection, **after** the
    workspace lifecycle lock is taken, and a value other than ``retention_days``
    refuses. A parameter rather than a constant because core declares no retention key
    of its own — ``recallatron.retention.days`` is a module's, and hardcoding it here
    would put a module's name in core.

    ``retained_successor_ref`` is the canonical reference of the replacement a
    superseded record was expired in favour of, and this is the only writer of that
    column anywhere: ``core.deletion_record``'s own check constraint refuses one on
    any cause but ``retention_expiry``.

    **The owner is told which disposition is asking**
    (:class:`~rheo_core.deletion.registry.Disposition`), and on this path that is the
    whole of how the deletion authorises itself: the context below carries no account,
    no audience and neither of the two roles an *erasure* authorizer admits, because a
    retention sweep is the workspace's own policy rather than a person. The owner's
    expiry branch is also where "is this record actually expired?" is asked — core
    knows nothing about a module's clock column — which is § A9's recheck under the
    lock, immediately before the delete.

    **One context per target, and the cost is deliberate.** The factory reads the
    control plane and the workspace's module states, so a hundred-root batch pays a
    hundred short reads on a second connection. The alternative is a caller-supplied
    context, which is exactly the parameter this whole value exists to avoid handing
    anyone.

    Returns :class:`~rheo_core.boundary.context.Refusal` rather than raising, for every
    refusal it owns: the caller is a job handler, a raise would fail the whole job, and
    a sweep that finds one record it may not expire has not failed.
    """
    # Deferred: this module is on ``work/loop.py``'s import path, and
    # ``rheo_core.deletion`` reaches ``rheo_core.operations``, which reaches back into
    # this package. The same deferral ``operations/core_ops.py`` and
    # ``approvals/gate.py`` already use to reach the coordinator. ``rheo_core.boundary``
    # is here for company rather than for a cycle.
    from rheo_core.boundary import context_for_memory_expiry
    from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
    from rheo_core.deletion.operations import (
        RECORD_DELETED,
        RECORD_DELETED_SCHEMA_VERSION,
    )
    from rheo_core.deletion.records import deletion_ref, insert_deletion_record
    from rheo_core.deletion.registry import (
        OWNED_DELETIONS,
        Disposition,
        RemovedMemories,
        authorize_owned_delete,
    )
    from rheo_core.deletion.tables import RETENTION_EXPIRY
    from rheo_core.events.publish import NewEvent, publish
    from rheo_core.operations import CONSUMERS_MISSING, OperationRefused

    registry = OWNED_DELETIONS
    conn = uow.connection
    if f"{target_ref.module}.{target_ref.record_type}" != record_type:
        return Refusal(
            EXPIRY_TYPE_MISMATCH,
            f"{target_ref.format()} is not a {record_type} record",
        )
    verified = sealed_execution(uow, capability)
    if isinstance(verified, Refusal):
        return verified
    # Prompt 2's guard, verbatim, and it is a raise rather than a refusal on purpose:
    # a worker wired with no registry is a deployment fault for every target in the
    # batch, not a record this sweep may not expire, so it must end the attempt rather
    # than be counted as one skipped row.
    consumers = uow.consumers if isinstance(uow, HandlerUnitOfWork) else None
    if consumers is None:
        raise OperationRefused(
            CONSUMERS_MISSING, "no ConsumerRegistry is wired for this dispatch"
        )
    # Built before the lock, because it opens a second connection of its own: taking
    # the lifecycle lock first would hold it across that checkout for no reason.
    ctx = context_for_memory_expiry(verified.workspace_id)
    if isinstance(ctx, Refusal):
        return ctx
    lock_workspace_lifecycle(conn)
    settled = resolve(
        workspace_id=verified.workspace_id,
        source=TransactionBoundOverrideSource(conn, workspace_id=verified.workspace_id),
    ).get_int(retention_key)
    if settled != retention_days:
        return Refusal(
            RETENTION_SETTING_CHANGED,
            f"{retention_key} is {settled}, not the {retention_days} this sweep "
            "selected its records against",
        )
    authorized = authorize_owned_delete(
        ctx,
        uow,
        target_ref,
        disposition=Disposition.SCHEDULED_EXPIRY,
        registry=registry,
    )
    if isinstance(authorized, Refusal):
        # An absent record, and one that is no longer expired, both land here — and
        # both are skips rather than failures: § A9 asks for neither a deletion record
        # nor a count for either, and returning before the insert is what gives it.
        return authorized
    owner, authorization = authorized
    removed: RemovedMemories = owner.delete_owned(ctx, uow, authorization)
    participants: list[str] = []
    for participant in registry.participants_for(
        target_ref, enabled_modules=ctx.enabled_modules
    ):
        # Uncaught, exactly as the approved coordinator leaves it: a participant that
        # raises takes the whole expiry with it and the record survives untouched.
        removed = removed.union(
            participant.handler(
                ctx, uow, target_ref, disposition=Disposition.SCHEDULED_EXPIRY
            )
        )
        participants.append(participant.module_id)
    # One reading for the ledger row and the event, so the two cannot disagree about
    # when the record was removed — the approved coordinator's own rule.
    now = datetime.now(UTC)
    deletion_id = insert_deletion_record(
        conn,
        ref=target_ref,
        actor_kind=ctx.actor.kind.value,
        actor_id=ctx.actor.id,
        # No approval, and that is the point of this path. The column is nullable for
        # exactly this cause and for no other.
        approval_id=None,
        participants=tuple(participants),
        invalidated_memory_count=len(removed.memory_ids),
        cause=RETENTION_EXPIRY,
        retained_successor_ref=retained_successor_ref,
        now=now,
    )
    reference = deletion_ref(deletion_id)
    publish(
        ctx,
        uow,
        NewEvent(
            type=RECORD_DELETED,
            schema_version=RECORD_DELETED_SCHEMA_VERSION,
            subject_ref=target_ref.format(),
            # The revision the owner authorised under the lock, exactly as the
            # approved coordinator reports it.
            subject_revision=authorization.revision,
            data={
                "ref": target_ref.format(),
                "deletion_record_ref": reference.format(),
            },
        ),
        now=now,
        consumers=consumers,
    )
    return ExpiredRecord(deletion_ref=reference.format())
