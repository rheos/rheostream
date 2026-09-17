"""``core.approval.approve`` and ``core.approval.refuse``: their declarations, models
and handlers, beside the repository they read.

**Both the declarations and the handlers live here**, which is a deliberate departure
from the split ``core.audit.list`` and the three ``core.operation`` operations use —
those keep their declaration in ``operations/core_ops.py`` and their models beside
their repository. The reason is the import direction stated in ``approvals/gate.py``:
``core_ops.py`` may only reach this package from *inside*
``register_core_operations()``, so a declaration there would have to be built inside
that function like the two token declarations already are. Keeping the pair together
here means the registrar imports two names instead of six and there is one place that
knows what these operations are.

**Who may approve is not checked here.** ``docs/architecture/
confirmation-and-safety.md`` § Who may approve says an authenticated account, through
the web interface, and the enforcement it names is the token policy: both names are in
``rheo_core.tokens.policy.NON_TOKEN_ISSUABLE``, so no ``cli``, ``mcp`` or ``runtime``
token can carry either, at issuance and again at presentation. A second check on
``ctx.entry`` here would be a rule with no failure it could catch that the first does
not, and it would refuse the one legitimate non-web caller — a test harness context —
on a technicality about how it was built.
"""

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)

from rheo_core.approvals import tables as t
from rheo_core.approvals.gate import execute_approved
from rheo_core.approvals.records import ApprovalRow, mark_approved, mark_refused
from rheo_core.approvals.records import (
    get as read_approval,
)
from rheo_core.operations.records import finish_held_cancelled
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.refs.resolver import NOT_FOUND
from rheo_core.storage.backend import UnitOfWork

APPROVAL_APPROVE: Final = "core.approval.approve"
APPROVAL_REFUSE: Final = "core.approval.refuse"
"""The two names, spelled whole.

They are the same two literals ``rheo_core.tokens.policy.NON_TOKEN_ISSUABLE`` has
carried since run 0b2, written there in anticipation of this chunk. Duplicated rather
than imported from each other, for the reason that module's own docstring gives: an
import either way closes a cycle, and two short literals are cheaper than one."""

APPROVAL_STATE: Final = "approval_state"
"""The refusal for an approval that is in the wrong state for the act asked of it.

Named after ``operation_ops.py``'s ``operation_state``, which is the same idea over
the record next door."""

# See ``rheo_core.operations.core_ops`` for why ``ignore`` (the default) is stated.
_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")

_APPROVAL_ROLES: Final = frozenset({Role.OWNER, Role.MEMBER})
"""``docs/architecture/module-contract.md``'s row for ``core.approval.approve`` and
``.refuse`` ratifies ``owner, member``. It happens to equal
``OperationDeclaration``'s own default, and is declared explicitly anyway so that a
later change to that default cannot silently re-role these two."""


class ApprovalRef(BaseModel):
    """Which approval to act on. ``approval_id`` is not a reserved input field, so a
    declaration may name it."""

    model_config = _IGNORE_EXTRA

    approval_id: UUID


class ApprovalRecord(BaseModel):
    """One approval as these operations publish it.

    :class:`~rheo_core.approvals.records.ApprovalRow` field for field, with that row's
    ``id`` published as ``approval_id`` — the renaming ``OperationRecord`` and
    ``AuditRecord`` already make for the same reason — and ``payload_digest``
    published as its lowercase hex, exactly as ``AuditRecord.request_digest`` is: the
    column is ``bytea``, a generated TypeScript client has no natural type for raw
    bytes, and hex is what a person comparing two digests can actually compare.

    **This is the supported read of the approval record** (AC 8). A caller reads the
    durable row — its state, who approved it, and the digest that binds it — off the
    operation that changed it, and never by querying ``core.approval``. The record is
    read back from the row *after* the transition, so what it publishes is what the
    database holds rather than what the handler intended to write.
    """

    model_config = ConfigDict(frozen=True)

    approval_id: UUID
    actor_kind: str
    actor_id: UUID | None
    operation_name: str
    operation_id: UUID
    destination_ref: str | None
    subject_ref: str | None
    subject_revision: int | None
    payload_digest: str
    purpose: str | None
    window_start: datetime
    window_end: datetime
    state: str
    approved_by_kind: str | None
    approved_by_id: UUID | None
    approved_at: datetime | None
    approved_entry: str | None
    executed_at: datetime | None
    invalidated_reason: str | None


def _published(row: ApprovalRow) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row.id,
        actor_kind=row.actor_kind,
        actor_id=row.actor_id,
        operation_name=row.operation_name,
        operation_id=row.operation_id,
        destination_ref=row.destination_ref,
        subject_ref=row.subject_ref,
        subject_revision=row.subject_revision,
        payload_digest=row.payload_digest.hex(),
        purpose=row.purpose,
        window_start=row.window_start,
        window_end=row.window_end,
        state=row.state,
        approved_by_kind=row.approved_by_kind,
        approved_by_id=row.approved_by_id,
        approved_at=row.approved_at,
        approved_entry=row.approved_entry,
        executed_at=row.executed_at,
        invalidated_reason=row.invalidated_reason,
    )


def _must_read(uow: UnitOfWork, approval_id: UUID) -> ApprovalRow:
    """The approval, or a ``not_found`` refusal naming it.

    The workspace is the context's own — ``uow.connection`` is already routed to it —
    so an id minted in another workspace is simply not here, which is the same answer
    a deleted one gets. ``operation_ops.py``'s ``_must_read`` is the shape and the
    reasoning.
    """
    row = read_approval(uow.connection, approval_id=approval_id)
    if row is None:
        raise OperationRefused(
            NOT_FOUND, f"no approval {approval_id} in this workspace"
        )
    return row


def _must_be_pending(row: ApprovalRow, approval_id: UUID) -> None:
    """Refuse unless the approval is still waiting for a decision.

    The read is what lets the refusal say *which* state the approval is in — already
    executed, already refused, and never approved are different situations and a
    person acts on them differently. The predicate on the write is what makes the
    refusal true anyway if the row moved between the two statements.
    """
    if row.state != t.PENDING:
        raise OperationRefused(
            APPROVAL_STATE,
            f"approval {approval_id} is {row.state!r}; only a 'pending' approval can "
            "be approved or refused",
        )


def approve_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: ApprovalRef
) -> ApprovalRecord:
    """Approve a pending approval, which runs the operation it was minted for.

    Everything happens in the caller's single transaction, in this order: the
    approval becomes ``approved``, recording who approved and through which entry;
    then :func:`~rheo_core.approvals.gate.execute_approved` rechecks the binding, runs
    the original handler, marks the approval ``executed``, terminalises the held
    operation record and writes that operation's audit row. A refusal anywhere in
    that sequence rolls all of it back, which is what leaves a failed approval
    re-approvable rather than half-spent — and is why the sequence is described rather
    than counted, since the count is the one thing about it that changes.

    ``datetime.now(UTC)`` is read **once** here and passed down, for the reason
    ``operation_ops.py``'s ``resolve_handler`` reads it: this is a handler reached
    through the dispatcher, which takes no instant. One read rather than several so
    that ``approved_at``, the window comparison, ``executed_at`` and the record's
    terminal instant are the same moment — a second read after an unbounded handler
    could put the window check and the execution on opposite sides of an expiry.
    """
    approval_id = model_input.approval_id
    row = _must_read(uow, approval_id)
    _must_be_pending(row, approval_id)
    now = datetime.now(UTC)
    if not mark_approved(
        uow.connection,
        approval_id=approval_id,
        approved_by_kind=ctx.actor.kind.value,
        approved_by_id=ctx.actor.id,
        approved_entry=ctx.entry.value,
        now=now,
    ):
        raise OperationRefused(
            APPROVAL_STATE,
            f"approval {approval_id} left 'pending' while it was being approved; "
            "nothing was written",
        )
    execute_approved(ctx, uow, approval=_must_read(uow, approval_id), now=now)
    return _published(_must_read(uow, approval_id))


def refuse_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: ApprovalRef
) -> ApprovalRecord:
    """Refuse a pending approval. The operation it was minted for never runs.

    The held ``core.operation`` record is ended ``cancelled`` in the same
    transaction. Leaving it ``approval_required`` would be the durable-status defect
    criterion 13 forbids one state over: a caller polling the record would wait for a
    decision that has already been made against it.
    """
    approval_id = model_input.approval_id
    row = _must_read(uow, approval_id)
    _must_be_pending(row, approval_id)
    if not mark_refused(uow.connection, approval_id=approval_id):
        raise OperationRefused(
            APPROVAL_STATE,
            f"approval {approval_id} left 'pending' while it was being refused; "
            "nothing was written",
        )
    if not finish_held_cancelled(
        uow.connection, operation_id=row.operation_id, now=datetime.now(UTC)
    ):
        raise OperationRefused(
            APPROVAL_STATE,
            f"the operation record {row.operation_id} left 'approval_required' "
            "while its approval was being refused",
        )
    return _published(_must_read(uow, approval_id))


APPROVE_DECLARATION: Final = OperationDeclaration(
    name=APPROVAL_APPROVE,
    safety_class=SafetyClass.MUTATE,
    roles=_APPROVAL_ROLES,
    input_model=ApprovalRef,
    output=ApprovalRecord,
    # Each approval is approved once: the second call finds the row no longer
    # ``pending`` and refuses. Nothing makes a repeat the same row, which is what
    # ``NATURAL`` would claim.
    idempotency=Idempotency.NONE,
    # ``MUTATE``, so an ``AuditSpec`` is required. ``subject_field=None`` because
    # the spec names an *input-model field carrying a RecordRef* and this input names
    # an approval id, which is not one — the same reason ``core.operation.resolve``
    # gives. The gated operation's own subject is on the row the approval carries and
    # on the audit row the execution writes for it.
    audit=AuditSpec(subject_field=None),
)

REFUSE_DECLARATION: Final = OperationDeclaration(
    name=APPROVAL_REFUSE,
    safety_class=SafetyClass.MUTATE,
    roles=_APPROVAL_ROLES,
    input_model=ApprovalRef,
    output=ApprovalRecord,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)

APPROVAL_OPERATIONS: Final[tuple[tuple[OperationDeclaration, Handler], ...]] = (
    (APPROVE_DECLARATION, approve_handler),
    (REFUSE_DECLARATION, refuse_handler),
)
"""The pair ``register_core_operations()`` registers, mirroring
``core_ops.py``'s own ``CORE_OPERATIONS`` tuple so a reader who knows one recognises
the other."""
