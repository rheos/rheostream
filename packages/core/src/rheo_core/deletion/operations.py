"""``core.record.delete``: the owned-deletion coordinator and the ledger it leaves.

The whole of R5's record-level deletion that this tree implements, and the shape of the
rest. One destructive, per-action-approved operation; one owner's delete; every enabled
participant; one content-free ledger row; one ``core.record.deleted`` event; and the
gated operation's own success audit row — **all in the transaction
``approvals/gate.py:execute_approved`` already holds**, so a participant that raises
takes the effect, the ledger, the event and the audit row with it and the record
survives untouched.

**No second commit boundary is built here, and that is deliberate.** ``dispatch()``
opens one unit of work, ``core.approval.approve``'s handler runs inside it,
``execute_approved`` runs this handler inside that, and a raised ``OperationRefused``
reaches ``dispatch()``'s own rollback. Adding a commit here would be a second boundary
for one atomicity rule.

**Three authorization checks, one function.** ``registry.authorize_owned_delete`` runs
before an approval is minted (from ``hold_for_approval``, so a reference core will not
delete never creates an approval at all), again here under the reconstructed original
caller's context, and a third time under the workspace lifecycle lock immediately
before the physical delete. The third is the one that closes the window: between the
second check and the effect, another transaction could have moved or removed the
record, and the lock plus a revision comparison is what stops an approver's release
from acting on something other than what was approved.

**What the caller gets back is ``{deletion_ref}`` and nothing else** (``docs/
architecture/deletion-export-migration.md``). No count, no participant list, no
ancestor label: the closure's size is a fact about other people's records, and an
owner who could read it off their own deletion would learn how much memory somebody
else's module held about their record. The exact counters live on the ledger row, which
release one exposes only to an owner or operator through a later opt-in collection.

**Three of the four counters are structurally zero here.** Cancelling queued jobs and
pending external actions and marking held exports is criterion 65's work and is not
built; ``records.py``'s writer holds them at zero and takes no parameter for them, so
nothing in this file can claim a cascade that did not run. This is not complete R5
coverage and does not claim to be.
"""

from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator
from rheo_contracts import OperationDeclaration, RecordRef, WorkspaceContext

from rheo_core.approvals.records import get_for_operation
from rheo_core.boundary.context import Refusal
from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
from rheo_core.deletion.records import deletion_ref, insert_deletion_record
from rheo_core.deletion.registry import (
    OWNED_DELETIONS,
    DeleteAuthorization,
    OwnedDeletion,
    OwnedDeletionRegistry,
    RemovedMemories,
    authorize_owned_delete,
)
from rheo_core.deletion.tables import USER_ERASURE
from rheo_core.events.publish import NewEvent, publish
from rheo_core.operations import CONSUMERS_MISSING, OperationRefused
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork

RECORD_DELETE: Final = "core.record.delete"
RECORD_DELETED: Final = "core.record.deleted"
RECORD_DELETED_SCHEMA_VERSION: Final = 1
"""``core.record.deleted``'s envelope version (``intake-and-events.md`` § Envelope).

Published directly rather than through an ``EventDeclaration``: ``core`` is the
reserved module segment and cannot declare anything through a ``ModuleManifest``, so
there is no manifest for this type to be declared in and the coordinator is its own
producer.
"""

DELETION_UNAPPROVED: Final = "deletion_unapproved"
"""The refusal for a ``core.record.delete`` reached outside an approved execution.

Unreachable through ``dispatch()``: the declaration is ``DESTRUCTIVE``, so the
dispatcher holds the call and never enters this handler, and
``NON_TOKEN_ISSUABLE``-style standing grants are refused for the class at grant time.
Refused rather than assumed, because the ledger's ``approval_id`` is what records that
a person confirmed this erasure, and a deletion with nothing to put there would be a
destructive act with no confirmation behind it.
"""

RECORD_MOVED: Final = "record_moved"
"""The refusal when the target moved between the approved check and the locked one.

Distinct from ``invalid_approval``, which ``execute_approved``'s binding check answers
for a payload that no longer matches, and from ``RecordStateGuard``'s refusal, which
compares the *resolver's* revision against the approval's. This one compares the
**owner's** authorised revision either side of the lifecycle lock, which is the only
comparison that spans the window the lock exists to close.
"""


class RecordDeleteInput(BaseModel):
    """``core.record.delete(ref)``.

    **The wire form is the canonical ``<module>.<record_type>:<uuid>`` string, and
    the validated field is a ``RecordRef``.** Both halves are load-bearing and the
    validator below is what makes them one field rather than two. The string is what
    the contract gives a caller and what ``recallatron_forget`` will pass; the typed
    value is what ``AuditSpec(subject_field="ref")`` needs, because
    ``dispatch``'s ``_subject_ref`` records a subject only for a field that *is* a
    ``RecordRef`` and would otherwise write a null ``subject_ref`` on every audit row
    of the one core operation that genuinely acts on a record — indistinguishable from
    the ordinary null of an operation that acts on none. ``RecordStateGuard`` reads the
    same field and would fail the same way.

    A malformed string raises ``RecordRefMalformed`` inside the validator, which
    pydantic reports as a validation error and ``dispatch()`` answers ``input_invalid``
    for — before authorization, before the hold, before anything is written.
    """

    model_config = ConfigDict(extra="ignore")

    ref: RecordRef

    @field_validator("ref", mode="before")
    @classmethod
    def _accept_the_canonical_string(cls, value: object) -> object:
        return RecordRef.parse(value) if isinstance(value, str) else value


class RecordDeleted(BaseModel):
    """The only thing any caller receives: the ledger row's own reference.

    One field, and no second one is coming. See the module docstring on why a count
    here would be a disclosure rather than a convenience.
    """

    model_config = ConfigDict(frozen=True)

    deletion_ref: str


def pre_mint_refusal(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    declaration: OperationDeclaration,
    model_input: BaseModel,
    *,
    registry: OwnedDeletionRegistry = OWNED_DELETIONS,
) -> Refusal | None:
    """The owned-delete authorization, run before an approval is minted.

    Called from ``approvals/gate.py:hold_for_approval`` inside the transaction that is
    about to mint, and answering a :class:`~rheo_core.boundary.context.Refusal` makes
    that function return before ``mint()`` — so a reference core will not delete leaves
    no ``core.operation`` row and no ``core.approval`` row behind, only the refused
    audit row every pre-transaction refusal writes.

    **``None`` for every declaration that is not this one**, which is every other
    operation in the tree: this is a hook with exactly one implementation, not a
    general pre-approval mechanism. It is keyed on the operation name rather than on
    the safety class, because ``harness.fixture.act`` is destructive too and names a
    subject, and running an owned-delete authorizer over it would refuse a fixture that
    deletes nothing.
    """
    if declaration.name != RECORD_DELETE or not isinstance(
        model_input, RecordDeleteInput
    ):
        return None
    authorized = authorize_owned_delete(ctx, uow, model_input.ref, registry=registry)
    return authorized if isinstance(authorized, Refusal) else None


def _authorized_or_refused(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    ref: RecordRef,
    registry: OwnedDeletionRegistry,
) -> tuple[OwnedDeletion, DeleteAuthorization]:
    """Run one authorization checkpoint, raising the refusal rather than returning it.

    A refusal inside the handler has to reach ``dispatch()``'s rollback, so it is
    raised as an ``OperationRefused`` carrying the refusal's own state — the shape
    ``execute_approved`` already uses for a rebuild that failed.
    """
    authorized = authorize_owned_delete(ctx, uow, ref, registry=registry)
    if isinstance(authorized, Refusal):
        raise OperationRefused(authorized.state, str(authorized))
    return authorized


def delete_owned(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: RecordDeleteInput
) -> RecordDeleted:
    """Delete one owned record and everything the workspace owns because of it.

    ``ctx`` is the **original held caller's** context, rebuilt by
    ``boundary/factories.py:context_for_approved_execution`` — never the approver's —
    so the owner's authorizer, the participants and the ledger's ``actor_*`` columns
    all see the person whose call was held.

    ``datetime.now(UTC)`` is read once here, for the ledger row and the event's
    ``occurred_at``. A handler is reached through the dispatcher, which takes no
    instant, and one read rather than two is what stops a ledger row and its event
    disagreeing about when the deletion happened.
    """
    registry = OWNED_DELETIONS
    ref = model_input.ref
    now = datetime.now(UTC)
    # Prompt 2's guard, verbatim: the composition root's one registry or a refusal,
    # never a per-call throwaway whose fan-out would depend on which call built it.
    consumers = uow.consumers if isinstance(uow, HandlerUnitOfWork) else None
    if consumers is None:
        raise OperationRefused(
            CONSUMERS_MISSING, "no ConsumerRegistry is wired for this dispatch"
        )
    operation_id = uow.operation_id if isinstance(uow, HandlerUnitOfWork) else None
    approval = (
        None
        if operation_id is None
        else get_for_operation(uow.connection, operation_id=operation_id)
    )
    if approval is None:
        raise OperationRefused(
            DELETION_UNAPPROVED,
            f"{RECORD_DELETE} runs only from an approved execution",
        )
    # The second checkpoint: the authorization re-run under the reconstructed original
    # caller, whose role and enabled modules may both have changed since the hold.
    _, approved = _authorized_or_refused(ctx, uow, ref, registry)
    # The third: under the lifecycle lock, immediately before the effect, so nothing
    # can move the target between the check and the delete.
    lock_workspace_lifecycle(uow.connection)
    owner, authorization = _authorized_or_refused(ctx, uow, ref, registry)
    if authorization.revision != approved.revision:
        raise OperationRefused(
            RECORD_MOVED,
            f"{ref.format()} is at revision {authorization.revision}, not the "
            f"revision {approved.revision} this execution authorised",
        )
    removed: RemovedMemories = owner.delete_owned(ctx, uow, authorization)
    participants: list[str] = []
    for participant in registry.participants_for(
        ref, enabled_modules=ctx.enabled_modules
    ):
        # A participant that raises aborts the whole deletion: it is not caught here,
        # so the exception reaches ``dispatch()`` and everything above rolls back.
        removed = removed.union(participant.handler(ctx, uow, ref))
        participants.append(participant.module_id)
    deletion_id = insert_deletion_record(
        uow.connection,
        ref=ref,
        actor_kind=ctx.actor.kind.value,
        actor_id=ctx.actor.id,
        approval_id=approval.id,
        participants=tuple(participants),
        # Distinct rows, unioned across the owner and every participant: two
        # participants reporting the same removed memory count it once.
        invalidated_memory_count=len(removed.memory_ids),
        cause=USER_ERASURE,
        # Never for an erasure. The table's own check constraint refuses a non-null
        # here under this cause, so the rule is the database's and not this line's.
        retained_successor_ref=None,
        now=now,
    )
    reference = deletion_ref(deletion_id)
    publish(
        ctx,
        uow,
        NewEvent(
            type=RECORD_DELETED,
            schema_version=RECORD_DELETED_SCHEMA_VERSION,
            subject_ref=ref.format(),
            # The revision the record held when it was erased, which is the revision
            # the owner authorised under the lock one statement earlier.
            subject_revision=authorization.revision,
            data={"ref": ref.format(), "deletion_record_ref": reference.format()},
        ),
        now=now,
        consumers=consumers,
    )
    return RecordDeleted(deletion_ref=reference.format())
