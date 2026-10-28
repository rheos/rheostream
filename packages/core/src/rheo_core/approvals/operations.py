"""``core.approval.preview``, ``core.approval.approve`` and ``core.approval.refuse``:
their declarations, models and handlers, beside the repository they read.

**Both the declarations and the handlers live here**, which is a deliberate departure
from the split ``core.audit.list`` and the three ``core.operation`` operations use —
those keep their declaration in ``operations/core_ops.py`` and their models beside
their repository. The reason is the import direction stated in ``approvals/gate.py``:
``core_ops.py`` may only reach this package from *inside*
``register_core_operations()``, so a declaration there would have to be built inside
that function like the two token declarations already are. Keeping the pair together
here means the registrar imports two names instead of six and there is one place that
knows what these operations are.

**No token reaches any of the three.** All three names are in
``rheo_core.tokens.policy.NON_TOKEN_ISSUABLE``, so no ``cli``, ``mcp`` or ``runtime``
token can carry one, at issuance and again at presentation. ``ctx.entry`` is not
checked: that would refuse the one legitimate non-web caller — a test harness context
— on a technicality about how it was built.

**Who may see or decide is checked here, against the current state of the world.**
``docs/architecture/confirmation-and-safety.md`` § Who may approve says an
authenticated person, and the run-4b2 spec adds that the person must still be allowed
to see what they are approving. :func:`_eligible` and :func:`_subject_visible` are that
rule, and the preview and both decisions share them: the context's actor is an account
that is its own authenticated principal, its ``control.membership`` row *now* carries
``owner`` or ``member`` (read afresh, so a context built before a revocation does not
outlive it), and the held call's subject — read from the **verified** snapshot, never
from the stored ``subject_ref`` column — resolves for this viewer.

**The preview is non-enumerating.** Every state in which a viewer may not be shown the
approval — no such row, an ineligible or unauthenticated viewer, a subject the viewer
cannot see, a snapshot that does not verify — answers with one identical redacted
:data:`REDACTED_PREVIEW`. A decision refused for eligibility answers ``not_found`` with
the same text a missing id gets.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)
from rheo_contracts import (
    ActorKind,
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    WorkspaceContext,
)

from rheo_core.approvals import tables as t
from rheo_core.approvals.binding import (
    INVALID_APPROVAL,
    executable_payload,
    payload_digest,
)
from rheo_core.approvals.gate import execute_approved
from rheo_core.approvals.records import (
    ApprovalRow,
    get_for_update,
    mark_approved,
    mark_refused,
    payload_body,
)
from rheo_core.approvals.records import (
    get as read_approval,
)
from rheo_core.operations.records import finish_held_cancelled
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import REGISTRY, Handler, OperationRegistry
from rheo_core.refs.resolver import NOT_FOUND, Unavailable, resolve_in
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import get_membership
from rheo_core.storage.postgres import get_backend

APPROVAL_APPROVE: Final = "core.approval.approve"
APPROVAL_REFUSE: Final = "core.approval.refuse"
APPROVAL_PREVIEW: Final = "core.approval.preview"
"""The three names, spelled whole.

The same literals ``rheo_core.tokens.policy.NON_TOKEN_ISSUABLE`` carries (approve and
refuse since run 0b2, preview since run 4b2). Duplicated rather than imported from
each other, for the reason that module's own docstring gives: an import either way
closes a cycle, and short literals are cheaper than one."""

APPROVAL_STATE: Final = "approval_state"
"""The refusal for an approval that is in the wrong state for the act asked of it.

Named after ``operation_ops.py``'s ``operation_state``, which is the same idea over
the record next door."""

APPROVAL_DIGEST_MISMATCH: Final = "approval_digest_mismatch"
"""The refusal for a decision whose ``expected_payload_digest`` is not the digest the
approval row binds.

The shell sends the digest it displayed; this refusal is what a preview-to-submit race
looks like — the row moved, or the page was showing something else — and nothing is
written. The assertion only ever narrows: it cannot replace the stored body, digest or
destination, which the decision verifies independently."""

# See ``rheo_core.operations.core_ops`` for why ``ignore`` (the default) is stated.
_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")

_APPROVAL_ROLES: Final = frozenset({Role.OWNER, Role.MEMBER})
"""``docs/architecture/module-contract.md``'s row for ``core.approval.approve`` and
``.refuse`` ratifies ``owner, member``. It happens to equal
``OperationDeclaration``'s own default, and is declared explicitly anyway so that a
later change to that default cannot silently re-role these two."""


class ApprovalRef(BaseModel):
    """Which approval to read. ``approval_id`` is not a reserved input field, so a
    declaration may name it."""

    model_config = _IGNORE_EXTRA

    approval_id: UUID


class ApprovalDecision(BaseModel):
    """Which approval to approve or refuse, and optionally the digest the person saw.

    ``expected_payload_digest`` is the lowercase hex ``core.approval.preview``
    published. When present it is compared with the row's digest under the row lock
    the decision holds, and a difference refuses :data:`APPROVAL_DIGEST_MISMATCH`.
    It is an assertion about the binding, never a substitute for it: the decision
    still verifies the stored snapshot against the stored digest on its own.
    """

    model_config = _IGNORE_EXTRA

    approval_id: UUID
    expected_payload_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


PreviewStatus = Literal["pending", "expired", "resolved", "invalid", "unavailable"]
"""The closed display status of :class:`ApprovalPreview`.

- ``pending`` — durable ``pending`` inside its window, verified, viewer eligible. The
  only status with ``actionable = true``.
- ``expired`` — durable ``expired``, or ``pending`` outside its window. Read-only
  history of the verified snapshot.
- ``resolved`` — durable ``approved`` (queued or executing), ``executed`` or
  ``refused``. Read-only history; ``durable_state`` says which.
- ``invalid`` — durable ``requires_reapproval`` or ``invalidated``: the binding is
  stale and a person must review a new approval. Redacted.
- ``unavailable`` — anything the viewer may not be shown. Redacted, and identical for
  every cause."""

_DISCLOSED: Final[frozenset[str]] = frozenset({"pending", "expired", "resolved"})


class ApprovalPreview(BaseModel):
    """What a person is shown before deciding, or the redacted answer.

    **Disclosed** (``pending``, ``expired``, ``resolved``): every field below is the
    verified persisted binding — the ids, the operation, who asked (the original held
    caller, never the viewer), the subject read from the verified payload, the exact
    canonical destination (null for a class with no destination), the complete
    validated payload, its digest as lowercase hex, the purpose and window, and the
    durable approval state. ``external_action_id`` is set for a staged KEYED
    EXTERNAL action, whose own outcome ``core.external_action.get`` reports.

    **Redacted** (``invalid``, ``unavailable``): every field but ``status`` and
    ``actionable`` is null. ``unavailable`` is byte-identical whatever the cause.

    ``actionable`` is true exactly when ``status`` is ``pending``. The validator makes
    every other combination unconstructable, so a handler bug cannot publish one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: PreviewStatus
    actionable: bool
    approval_id: UUID | None = None
    operation_id: UUID | None = None
    external_action_id: UUID | None = None
    operation_name: str | None = None
    requester_kind: str | None = None
    requester_id: UUID | None = None
    subject_ref: str | None = None
    destination_ref: str | None = None
    payload: dict[str, JsonValue] | None = None
    payload_digest: str | None = None
    purpose: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    durable_state: str | None = None

    @model_validator(mode="after")
    def _closed(self) -> "ApprovalPreview":
        if self.actionable != (self.status == "pending"):
            raise ValueError("actionable is true exactly for a pending preview")
        details = self.model_dump(exclude={"status", "actionable"})
        if self.status in _DISCLOSED:
            required = (
                "approval_id",
                "operation_id",
                "operation_name",
                "requester_kind",
                "payload",
                "payload_digest",
                "window_start",
                "window_end",
                "durable_state",
            )
            missing = [name for name in required if details[name] is None]
            if missing:
                raise ValueError(f"a disclosed preview needs {', '.join(missing)}")
        elif any(value is not None for value in details.values()):
            raise ValueError(f"a {self.status} preview discloses nothing")
        return self


REDACTED_PREVIEW: Final = ApprovalPreview(status="unavailable", actionable=False)
"""The one answer for every approval the viewer may not be shown."""

_INVALID_PREVIEW: Final = ApprovalPreview(status="invalid", actionable=False)


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


def _not_available(approval_id: UUID) -> OperationRefused:
    """The refusal a missing approval and an undecidable-by-this-viewer one share.

    One text for both, so a decision attempt cannot tell an ineligible caller whether
    the id exists. It is :func:`_must_read`'s own wording.
    """
    return OperationRefused(NOT_FOUND, f"no approval {approval_id} in this workspace")


def _eligible(ctx: WorkspaceContext) -> bool:
    """Whether ``ctx`` is a person who may currently see and decide approvals.

    An account actor that is its own authenticated principal, with the context's role
    and a **freshly read** ``control.membership`` role both in ``owner, member``. The
    membership is read on the control engine at the moment of the call rather than
    trusted from the context, so a person who lost the membership after their session
    was resolved is refused rather than carried by a stale context. A token, operator,
    system or connection actor is never eligible; the token policy refuses tokens
    first anyway.
    """
    actor = ctx.actor
    if actor.kind is not ActorKind.ACCOUNT or actor.id is None:
        return False
    if ctx.principal.account_id != actor.id or ctx.role not in _APPROVAL_ROLES:
        return False
    with get_backend().control_engine.connect() as connection:
        membership = get_membership(
            connection, account_id=actor.id, workspace_id=ctx.workspace_id
        )
    return membership is not None and membership.role in _APPROVAL_ROLES


@dataclass(frozen=True, slots=True)
class VerifiedSnapshot:
    """An approval's stored snapshot, proved to be the bound one.

    ``payload`` is the validated input as it will execute, which is also the stored
    body byte for byte (both digests are checked). ``subject`` comes from that
    payload's declared subject field, never from the stored ``subject_ref`` column.
    """

    declaration: OperationDeclaration
    model_input: BaseModel
    payload: dict[str, object]
    subject: RecordRef | None
    destination_ref: str | None


def verify_snapshot(
    uow: UnitOfWork, row: ApprovalRow, *, registry: OperationRegistry = REGISTRY
) -> VerifiedSnapshot | None:
    """The approval's snapshot if it verifies against its binding, else ``None``.

    Verified means all of: the operation is registered here; the snapshot is stored;
    it validates against the declared input; the canonical digest of the stored body
    **and** of the validated input both equal the row's digest (so no ignored or
    defaulted field can differ from what is shown); and the destination binding is
    exact — for a declaration with an ``ExternalSpec`` the row and the payload both
    name exactly its declared destination, and for any other class the row names
    none. Read-only.
    """
    operation = registry.lookup(row.operation_name)
    if operation is None:
        return None
    declaration = operation.declaration
    body = payload_body(uow.connection, approval_id=row.id)
    if body is None:
        return None
    try:
        model_input = declaration.input_model.model_validate(body)
    except ValidationError:
        return None
    payload = executable_payload(model_input)
    if payload_digest(body) != row.payload_digest:
        return None
    if payload_digest(payload) != row.payload_digest:
        return None
    external = declaration.external
    if external is None:
        if row.destination_ref is not None:
            return None
    elif (
        row.destination_ref != external.destination_ref
        or getattr(model_input, external.destination_field, None)
        != external.destination_ref
    ):
        return None
    subject: RecordRef | None = None
    subject_field = (
        None if declaration.audit is None else declaration.audit.subject_field
    )
    if subject_field is not None:
        value = getattr(model_input, subject_field, None)
        if value is not None and not isinstance(value, RecordRef):
            return None
        subject = value
    return VerifiedSnapshot(
        declaration=declaration,
        model_input=model_input,
        payload=payload,
        subject=subject,
        destination_ref=row.destination_ref,
    )


def _subject_visible(
    ctx: WorkspaceContext, uow: UnitOfWork, verified: VerifiedSnapshot
) -> bool:
    """Whether the viewer can currently see the held call's subject.

    True when the call names no subject. Otherwise the subject must resolve for this
    viewer's own context — the module enabled and ready, the resolver answering for
    this actor and audience. A deleted-but-resolving subject is still visible: the
    viewer may see that it is gone. Read-only.

    **One ratified exception: erasure authority.** ``core.record.delete`` keeps
    erasure authority distinct from read authority (``docs/architecture/memory.md``,
    the member-audience removal path): an owner may erase a member-audience memory it
    cannot read, so a departed member's memories stay removable. For that one
    operation a viewer who
    passes the same owned-delete authorizer that gates minting the hold — the
    authority to have held this deletion themselves — may see and decide it. What
    the preview then discloses is the reference and the requester, never the
    record's content. Every other operation needs the subject to resolve.
    """
    if verified.subject is None:
        return True
    if not isinstance(resolve_in(verified.subject, ctx, uow), Unavailable):
        return True
    # Deferred for the import direction ``approvals/gate.py`` documents.
    from rheo_core.deletion.operations import RECORD_DELETE, pre_mint_refusal

    if verified.declaration.name != RECORD_DELETE:
        return False
    return (
        pre_mint_refusal(ctx, uow, verified.declaration, verified.model_input) is None
    )


def _display_status(row: ApprovalRow, now: datetime) -> PreviewStatus:
    """The closed display status of an eligible viewer's verified approval."""
    if row.state == t.PENDING:
        inside = row.window_start <= now <= row.window_end
        return "pending" if inside else "expired"
    if row.state == t.EXPIRED:
        return "expired"
    if row.state in (t.APPROVED, t.EXECUTED, t.REFUSED):
        return "resolved"
    if row.state in (t.REQUIRES_REAPPROVAL, t.INVALIDATED):
        return "invalid"
    return "unavailable"


def _lock_for_decision(
    ctx: WorkspaceContext, uow: UnitOfWork, approval_id: UUID
) -> ApprovalRow:
    """Lock the approval row, then refuse unless this viewer may decide it.

    The lock comes first so every later check — state, eligibility, snapshot, the
    displayed digest — is judged against a row nothing else can move until this
    transaction ends. An ineligible viewer gets the same ``not_found`` a missing id
    gets, before learning the approval's state.
    """
    row = get_for_update(uow.connection, approval_id=approval_id)
    if row is None or not _eligible(ctx):
        raise _not_available(approval_id)
    return row


def _must_be_pending(row: ApprovalRow, approval_id: UUID) -> None:
    """Refuse unless the approval is still waiting for a decision.

    ``pending`` is the only decidable state. Already executed, already refused and
    approved-and-queued are different situations a person acts on differently, so the
    refusal names the state. ``requires_reapproval`` (a restored, stale binding) and
    ``expired``/``invalidated`` are refused too: a person reviews a new approval
    rather than releasing or refusing this one, and nothing is rebuilt. The predicate
    on the write keeps the refusal true if the row somehow moved anyway.
    """
    if row.state != t.PENDING:
        raise OperationRefused(
            APPROVAL_STATE,
            f"approval {approval_id} is {row.state!r}; only a pending approval can "
            "be approved or refused",
        )


def _assert_displayed(row: ApprovalRow, expected: str | None) -> None:
    """Refuse when the digest the person was shown is not the row's digest."""
    if expected is not None and expected != row.payload_digest.hex():
        raise OperationRefused(
            APPROVAL_DIGEST_MISMATCH,
            f"approval {row.id} binds a different payload digest from the one "
            "displayed; reload the approval before deciding",
        )


def preview_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: ApprovalRef
) -> ApprovalPreview:
    """The verified persisted binding for an eligible viewer, or the redacted answer.

    Read-only: one row read, one snapshot read, the subject's resolver, and the staged
    action read. It never stages, refreshes, edits, approves or executes anything, and
    an expired or stale approval is reported, never rebuilt.
    """
    if not _eligible(ctx):
        return REDACTED_PREVIEW
    row = read_approval(uow.connection, approval_id=model_input.approval_id)
    if row is None:
        return REDACTED_PREVIEW
    verified = verify_snapshot(uow, row)
    if verified is None or not _subject_visible(ctx, uow, verified):
        return REDACTED_PREVIEW
    status = _display_status(row, datetime.now(UTC))
    if status == "unavailable":
        return REDACTED_PREVIEW
    if status == "invalid":
        return _INVALID_PREVIEW
    from rheo_core.operations.external_actions import get_for_approval

    staged = get_for_approval(uow.connection, approval_id=row.id)
    return ApprovalPreview(
        status=status,
        actionable=status == "pending",
        approval_id=row.id,
        operation_id=row.operation_id,
        external_action_id=None if staged is None else staged.id,
        operation_name=row.operation_name,
        requester_kind=row.actor_kind,
        requester_id=row.actor_id,
        subject_ref=None if verified.subject is None else verified.subject.format(),
        destination_ref=verified.destination_ref,
        # JSON by construction (``model_dump(mode="json")``); the field revalidates it.
        payload=cast(dict[str, JsonValue], verified.payload),
        payload_digest=row.payload_digest.hex(),
        purpose=row.purpose,
        window_start=row.window_start,
        window_end=row.window_end,
        durable_state=row.state,
    )


def approve_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: ApprovalDecision
) -> ApprovalRecord:
    """Approve a pending approval, which runs the operation it was minted for.

    **Before anything is written**, under the approval's row lock and in this order:
    the row exists and the viewer is eligible (else ``not_found``, the same answer for
    both); it is ``pending`` (else ``approval_state``); the instant is inside its
    window and the stored snapshot verifies against its digest and exact destination
    (else ``invalid_approval``); the verified subject resolves for this viewer (else
    ``not_found``); and the optional displayed digest equals the row's (else
    ``approval_digest_mismatch``). Every one of these refuses before a job is queued
    or an inline effect runs, on the DESTRUCTIVE inline path and the EXTERNAL
    queueing path alike, so a stale page, a revoked membership or an expired or
    restored approval cannot release anything and nothing is rebuilt.

    Then everything happens in the caller's single transaction, in this order: the
    approval becomes ``approved``, recording who approved and through which entry;
    then :func:`~rheo_core.approvals.gate.execute_approved` rechecks the binding, runs
    the guards, runs the original handler, marks the approval ``executed``,
    terminalises the held operation record and writes that operation's audit row. A
    refusal anywhere in that sequence rolls all of it back, which is what leaves a
    failed approval re-approvable rather than half-spent — and is why the sequence is
    described rather than counted, since the count is the one thing about it that
    changes.

    **An execution guard's refusal is the deliberate exception and does not roll
    back.** It ends the approval ``refused`` and the held record ``failed``, and this
    operation returns those states rather than a refusal, because a rolled-back guard
    refusal would leave the approval ``pending`` and immediately re-approvable — which
    is precisely what R3 item 4 wants a revocation to stop. The caller reads
    ``state`` on the record below. ``rheo_core.approvals.gate.GUARD_REFUSED`` carries
    the whole reasoning.

    ``datetime.now(UTC)`` is read **once** here and passed down, for the reason
    ``operation_ops.py``'s ``resolve_handler`` reads it: this is a handler reached
    through the dispatcher, which takes no instant. One read rather than several so
    that ``approved_at``, the window comparison, ``executed_at`` and the record's
    terminal instant are the same moment — a second read after an unbounded handler
    could put the window check and the execution on opposite sides of an expiry.
    """
    approval_id = model_input.approval_id
    row = _lock_for_decision(ctx, uow, approval_id)
    _must_be_pending(row, approval_id)
    now = datetime.now(UTC)
    if not row.window_start <= now <= row.window_end:
        raise OperationRefused(
            INVALID_APPROVAL, "the approval's execution window has passed"
        )
    verified = verify_snapshot(uow, row)
    if verified is None:
        raise OperationRefused(
            INVALID_APPROVAL,
            f"the stored snapshot of approval {approval_id} does not verify against "
            "its digest and destination",
        )
    if not _subject_visible(ctx, uow, verified):
        raise _not_available(approval_id)
    _assert_displayed(row, model_input.expected_payload_digest)
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
            f"approval {approval_id} left its decidable state while approval was "
            "being recorded; nothing was written",
        )
    # The return value is deliberately discarded, and it has two shapes. The handler's
    # own output belongs to the gated operation and never to this one, which publishes
    # the approval; and a ``GuardRefusal`` has already written the approval ``refused``
    # and the held record ``failed`` in this transaction, so the record read back below
    # reports it. Reading the state off the row rather than off this value is what
    # makes the published answer the database's and not the handler's.
    #
    # A staged KEYED EXTERNAL/FINANCIAL action never runs here: approval rechecks the
    # binding, the held caller and the guards, then queues its one continuation job,
    # and the grant-bound continuation performs the effect. DESTRUCTIVE (and the
    # legacy unkeyed hold) still execute inline.
    from rheo_core.operations.external_actions import get_for_approval

    approved = _must_read(uow, approval_id)
    staged = get_for_approval(uow.connection, approval_id=approval_id)
    if staged is not None:
        from rheo_core.operations.continuation import queue_approved_external

        queue_approved_external(ctx, uow, approval=approved, action=staged, now=now)
    else:
        execute_approved(ctx, uow, approval=approved, now=now)
    return _published(_must_read(uow, approval_id))


def refuse_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: ApprovalDecision
) -> ApprovalRecord:
    """Refuse a pending approval. The operation it was minted for never runs.

    The same row lock, eligibility, ``pending``-only and displayed-digest checks as
    :func:`approve_handler`, all before anything is written. Two deliberate
    differences: a pending approval past its window may still be refused, and one
    whose snapshot no longer verifies may still be refused — refusal never runs
    anything, and refusing a tampered approval is the safe direction. When the
    snapshot does verify, its subject must be visible to the viewer.

    The held ``core.operation`` record is ended ``cancelled`` in the same
    transaction. Leaving it ``approval_required`` would be the durable-status defect
    criterion 13 forbids one state over: a caller polling the record would wait for a
    decision that has already been made against it.
    """
    approval_id = model_input.approval_id
    row = _lock_for_decision(ctx, uow, approval_id)
    _must_be_pending(row, approval_id)
    verified = verify_snapshot(uow, row)
    if verified is not None and not _subject_visible(ctx, uow, verified):
        raise _not_available(approval_id)
    _assert_displayed(row, model_input.expected_payload_digest)
    if not mark_refused(uow.connection, approval_id=approval_id):
        raise OperationRefused(
            APPROVAL_STATE,
            f"approval {approval_id} left its decidable state while refusal was "
            "being recorded; nothing was written",
        )
    now = datetime.now(UTC)
    if not finish_held_cancelled(
        uow.connection, operation_id=row.operation_id, now=now
    ):
        raise OperationRefused(
            APPROVAL_STATE,
            f"the operation record {row.operation_id} left 'approval_required' "
            "while its approval was being refused",
        )
    from rheo_core.operations.external_actions import get_for_approval

    staged = get_for_approval(uow.connection, approval_id=approval_id)
    if staged is not None:
        # The pre-job refusal path: the staged action and its keyed reservation are
        # refused with the approval, and no job or destination call ever exists.
        from rheo_core.operations.continuation import refuse_staged_external

        refuse_staged_external(uow.connection, action=staged, now=now)
    return _published(_must_read(uow, approval_id))


APPROVE_DECLARATION: Final = OperationDeclaration(
    name=APPROVAL_APPROVE,
    safety_class=SafetyClass.MUTATE,
    roles=_APPROVAL_ROLES,
    input_model=ApprovalDecision,
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
    input_model=ApprovalDecision,
    output=ApprovalRecord,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)

PREVIEW_DECLARATION: Final = OperationDeclaration(
    name=APPROVAL_PREVIEW,
    safety_class=SafetyClass.READ,
    # The same people who may decide: the preview is the decision's first half.
    roles=_APPROVAL_ROLES,
    input_model=ApprovalRef,
    output=ApprovalPreview,
    idempotency=Idempotency.NONE,
    audit=None,
)

APPROVAL_OPERATIONS: Final[tuple[tuple[OperationDeclaration, Handler], ...]] = (
    (PREVIEW_DECLARATION, preview_handler),
    (APPROVE_DECLARATION, approve_handler),
    (REFUSE_DECLARATION, refuse_handler),
)
"""The three ``register_core_operations()`` registers, mirroring
``core_ops.py``'s own ``CORE_OPERATIONS`` tuple so a reader who knows one recognises
the other."""
