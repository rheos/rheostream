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
whatever could call it. So :func:`dispatch_verified_expiry_in` does not trust the
object it is handed: it refuses anything that is not the very capability the view it
was given already carries, and then re-derives every fact on that value against the
live transaction before it deletes anything. A forged capability buys nothing, because
what is checked is the database's answer and not the object's.

``dispatch()`` gains no parameter for this, ever. A keyword on the public dispatcher
would be exactly the channel a caller could route an approval bypass through; the only
producer is the worker, and the only consumer is the entry below.

**What this module does not do.** It publishes no ``core.record.deleted`` event: the
worker's own ``HandlerUnitOfWork`` carries no ``ConsumerRegistry`` (``work/loop.py``
builds the view bare, and threading the worker's composition-root registry into it is
not this prompt's), so an expiry that tried to publish would refuse
``consumers_missing`` and take the deletion with it. The ledger row is written; the
event is the run that wires the worker's registry.
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
    :func:`dispatch_verified_expiry_in` re-derives all of them. Nothing here is a
    decision, a permission or a payload: a capability says what *was true*, and the
    entry that consumes it decides what that allows.
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


def dispatch_verified_expiry_in(
    uow: HandlerUnitOfWork,
    capability: VerifiedScheduledExecution,
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

    Returns :class:`~rheo_core.boundary.context.Refusal` rather than raising, for every
    refusal it owns: the caller is a job handler, a raise would fail the whole job, and
    a sweep that finds one record it may not expire has not failed.
    """
    # Deferred: this module is on ``work/loop.py``'s import path, and
    # ``rheo_core.deletion`` reaches ``rheo_core.operations``, which reaches back into
    # this package. The same deferral ``operations/core_ops.py`` and
    # ``approvals/gate.py`` already use to reach the coordinator. ``rheo_core.boundary``
    # is here for company rather than for a cycle.
    from rheo_core.boundary import context_for_operator
    from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
    from rheo_core.deletion.records import deletion_ref, insert_deletion_record
    from rheo_core.deletion.registry import (
        OWNED_DELETIONS,
        RemovedMemories,
        authorize_owned_delete,
    )
    from rheo_core.deletion.tables import RETENTION_EXPIRY

    registry = OWNED_DELETIONS
    if not isinstance(capability, VerifiedScheduledExecution):
        return Refusal(
            SCHEDULED_AUTHORITY_INVALID,
            "a verified scheduled execution is required to expire a record",
        )
    if capability is not uow.scheduled_execution:
        # Identity, not equality: the capability has to be the one the worker minted
        # onto this very view. An equal copy would mean a handler could assemble the
        # facts itself, which is the whole thing this value exists to prevent.
        return Refusal(
            SCHEDULED_AUTHORITY_INVALID,
            "the capability is not the one this unit of work carries",
        )
    conn = uow.connection
    if f"{target_ref.module}.{target_ref.record_type}" != record_type:
        return Refusal(
            EXPIRY_TYPE_MISMATCH,
            f"{target_ref.format()} is not a {record_type} record",
        )
    if not _still_verified(conn, capability):
        return Refusal(
            SCHEDULED_AUTHORITY_INVALID,
            "this is no longer a verified scheduled execution",
        )
    # Built before the lock, because it opens a second connection of its own: taking
    # the lifecycle lock first would hold it across that checkout for no reason. An
    # operator context is what a schedule is — the deployment's own timer acting on
    # nobody's behalf — and it is the only factory whose audience is ``None``, which
    # ``overview.md`` records as what a scheduled job carries.
    ctx = context_for_operator(capability.workspace_id)
    if isinstance(ctx, Refusal):
        return ctx
    lock_workspace_lifecycle(conn)
    settled = resolve(
        workspace_id=capability.workspace_id,
        source=TransactionBoundOverrideSource(
            conn, workspace_id=capability.workspace_id
        ),
    ).get_int(retention_key)
    if settled != retention_days:
        return Refusal(
            RETENTION_SETTING_CHANGED,
            f"{retention_key} is {settled}, not the {retention_days} this sweep "
            "selected its records against",
        )
    authorized = authorize_owned_delete(ctx, uow, target_ref, registry=registry)
    if isinstance(authorized, Refusal):
        return authorized
    owner, authorization = authorized
    removed: RemovedMemories = owner.delete_owned(ctx, uow, authorization)
    participants: list[str] = []
    for participant in registry.participants_for(
        target_ref, enabled_modules=ctx.enabled_modules
    ):
        # Uncaught, exactly as the approved coordinator leaves it: a participant that
        # raises takes the whole expiry with it and the record survives untouched.
        removed = removed.union(participant.handler(ctx, uow, target_ref))
        participants.append(participant.module_id)
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
        now=datetime.now(UTC),
    )
    return ExpiredRecord(deletion_ref=deletion_ref(deletion_id).format())
