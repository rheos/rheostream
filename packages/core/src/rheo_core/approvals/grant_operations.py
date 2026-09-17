"""``core.standing_grant.create`` and ``core.standing_grant.revoke``: their
declarations, models and handlers, beside the repository they read.

**Both the declarations and the handlers live here**, for the reason
``approvals/operations.py`` gives for the approve/refuse pair: ``core_ops.py`` may
only reach this package from *inside* ``register_core_operations()``, so a declaration
there would have to be built inside that function. Keeping the pair together here
means the registrar imports one name instead of four.

**What a grant may name, and what refuses it** (``docs/architecture/
confirmation-and-safety.md`` § Standing grants): "Every listed operation must be of
class ``read``, ``draft``, or ``mutate``; a destructive, external, or financial
operation is refused at grant time naming it." The refusal is at *grant* time and not
at use time, and that ordering is the whole point — a grant that could be written and
then quietly ignored for the three upper classes would read, to whoever wrote it, like
a permission they hold.

**Nothing consults a grant in release one.** The dispatcher never consults grants for
the three upper classes at all (that is ratified, not an omission), and no lower-class
call in this phase carries a confirmation requirement a grant could satisfy. So these
two operations write and revoke a durable record whose meaning — which operations it
covers, and from when to when — is published on the record itself
(:meth:`~rheo_core.approvals.grants.GrantRow.covers`) and read by nothing else yet.
"""

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from rheo_contracts import (
    ActorKind,
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)

from rheo_core.approvals import grants
from rheo_core.approvals.guards import GUARDED_CLASSES
from rheo_core.operations.refusals import OPERATION_UNKNOWN, OperationRefused
from rheo_core.operations.registry import REGISTRY, Handler
from rheo_core.refs.resolver import NOT_FOUND
from rheo_core.storage.backend import UnitOfWork

STANDING_GRANT_CREATE: Final = "core.standing_grant.create"
STANDING_GRANT_REVOKE: Final = "core.standing_grant.revoke"
"""The two names, spelled whole.

They are the same two literals ``rheo_core.tokens.policy.NON_TOKEN_ISSUABLE`` has
carried since run 0b2, written there in anticipation of this chunk, so AC 10's
enforcement — no ``cli``, ``mcp`` or ``runtime`` token may carry either, at issuance
and again at presentation — is already live for both. Duplicated rather than imported
from each other, for the reason that module's own docstring gives: an import either
way closes a cycle."""

STANDING_GRANT_CLASS_REFUSED: Final = "standing_grant_class_refused"
"""The refusal for a grant naming a destructive, external or financial operation.

Its detail names the offending operation, which is what the criterion asks for: a
caller who listed twenty operations learns which one it was rather than that one of
them was wrong."""

STANDING_GRANT_STATE: Final = "standing_grant_state"
"""The refusal for a grant that is in the wrong state for the act asked of it — today,
a revoke of a grant that is already revoked. Named after
``approvals/operations.py``'s ``approval_state``, which is the same idea over the
record next door."""

STANDING_GRANT_EXPIRY_REFUSED: Final = "standing_grant_expiry_refused"
"""The refusal for a requested lifetime that has already ended."""

# See ``rheo_core.operations.core_ops`` for why ``ignore`` (the default) is stated.
_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")

_GRANT_ROLES: Final = frozenset({Role.OWNER})
"""``docs/architecture/module-contract.md``'s row for the pair ratifies ``owner``.
``OperationDeclaration``'s own default is ``{owner, member}``, so leaving this off
would silently ship the wrong pair rather than fail."""


class StandingGrantCreate(BaseModel):
    """Who the grant is for, and what it covers.

    **The field is ``account_id``, not ``actor_id``, and it has to be.** ``actor_id``
    is one of ``RESERVED_INPUT_FIELDS`` (``rheo_contracts.manifest``) and a declaration
    naming it is refused at registration — criterion 6's rule that workspace, actor and
    storage identity come from the context only. ``core.standing_grant.create`` is the
    rare operation that legitimately names an account *other* than the caller's, and
    ``core.token.issue`` already ships the spelling for that case: ``account_id``. So
    the stored ``core.standing_grant.actor_id`` column is filled from an input field
    called ``account_id``, and the stored ``actor_kind`` is ``account``, which is the
    only actor kind a caller can name by id here.

    ``operation_names`` carries at least one name — a grant covering nothing is a row
    that says nothing, and pydantic refusing it costs no new refusal vocabulary.
    """

    model_config = _IGNORE_EXTRA

    account_id: UUID
    operation_names: list[str] = Field(min_length=1)
    expires_at: AwareDatetime


class StandingGrantRef(BaseModel):
    """Which grant to act on."""

    model_config = _IGNORE_EXTRA

    grant_id: UUID


class StandingGrantRecord(BaseModel):
    """One standing grant as these operations publish it.

    :class:`~rheo_core.approvals.grants.GrantRow` field for field, with that row's
    ``id`` published as ``grant_id`` — the renaming ``ApprovalRecord`` and
    ``OperationRecord`` already make for the same reason — plus ``covers``.

    **``covers`` is the published answer to "what does this grant cover now".** It is
    the grant's operation list while the grant is live and empty once it is revoked or
    expired, so a caller reads coverage from a supported operation instead of reading
    ``revoked_at`` and inferring what a timestamp implies. Both fields are published:
    ``operation_names`` is the record of what was granted, which a revocation does not
    erase, and ``covers`` is what it is worth today.
    """

    model_config = ConfigDict(frozen=True)

    grant_id: UUID
    actor_kind: str
    actor_id: UUID
    granted_by_id: UUID | None
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    operation_names: list[str]
    covers: list[str]


def _published(row: grants.GrantRow, now: datetime) -> StandingGrantRecord:
    return StandingGrantRecord(
        grant_id=row.id,
        actor_kind=row.actor_kind,
        actor_id=row.actor_id,
        granted_by_id=row.granted_by_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        operation_names=list(row.operation_names),
        covers=list(row.covers(now)),
    )


def _must_read(uow: UnitOfWork, grant_id: UUID, now: datetime) -> grants.GrantRow:
    """The grant, or a ``not_found`` refusal naming it.

    The workspace is the context's own — ``uow.connection`` is already routed to it —
    so an id minted in another workspace is simply not here, which is the same answer
    a grant that never existed gets. ``approvals/operations.py``'s ``_must_read`` is
    the shape and the reasoning.
    """
    row = grants.get(uow.connection, grant_id=grant_id)
    if row is None:
        raise OperationRefused(
            NOT_FOUND, f"no standing grant {grant_id} in this workspace"
        )
    return row


def _refuse_upper_class(names: list[str]) -> None:
    """Refuse the first named operation that is of a class a grant may never cover.

    Checked against the **live registry**, not against a list written here: the class
    is a property of the declaration, and a check that read anything else would
    approve a grant naming an operation whose class had since changed. An operation the
    registry does not know refuses too — its class cannot be read, and a grant naming a
    name nobody has registered could be a grant over a destructive operation that has
    not been loaded yet.

    Sorted so that "the first offending operation" is the same one on every run of the
    same input, which is what lets a refusal's text be asserted.
    """
    for name in sorted(set(names)):
        operation = REGISTRY.lookup(name)
        if operation is None:
            raise OperationRefused(
                OPERATION_UNKNOWN,
                f"{name} is not a registered operation, so a standing grant cannot be "
                "checked against its safety class",
            )
        safety_class = operation.declaration.safety_class
        if safety_class in GUARDED_CLASSES:
            raise OperationRefused(
                STANDING_GRANT_CLASS_REFUSED,
                f"{name} is of class {safety_class.value!r}; a standing grant may "
                "only cover read, draft and mutate operations, and the three upper "
                "classes are never satisfied by one",
            )


def create_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: StandingGrantCreate
) -> StandingGrantRecord:
    """Write a standing grant, or refuse it naming the operation that cannot be in it.

    Validation runs **before** anything is written, so a refused grant leaves no
    row behind at all — including no row naming the operations that were acceptable.
    A partially written grant would be a permission nobody asked for.
    """
    now = datetime.now(UTC)
    if model_input.expires_at <= now:
        raise OperationRefused(
            STANDING_GRANT_EXPIRY_REFUSED,
            "standing grant expires_at must be later than the creation time",
        )
    _refuse_upper_class(model_input.operation_names)
    grant_id = grants.create(
        uow.connection,
        actor_kind=ActorKind.ACCOUNT.value,
        actor_id=model_input.account_id,
        granted_by_id=ctx.actor.id,
        names=model_input.operation_names,
        created_at=now,
        expires_at=model_input.expires_at,
    )
    return _published(_must_read(uow, grant_id, now), now)


def revoke_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: StandingGrantRef
) -> StandingGrantRecord:
    """Revoke a live grant. The record of what it covered stays; the coverage stops.

    The read before the write is what lets the refusal say the grant was *already*
    revoked rather than that it is missing; the predicate on the write is what makes
    the refusal true anyway if the row moved between the two statements.
    """
    grant_id = model_input.grant_id
    now = datetime.now(UTC)
    row = _must_read(uow, grant_id, now)
    if row.revoked_at is not None:
        raise OperationRefused(
            STANDING_GRANT_STATE,
            f"standing grant {grant_id} was revoked at "
            f"{row.revoked_at.isoformat()} and covers nothing already",
        )
    if not grants.revoke(uow.connection, grant_id=grant_id, now=now):
        raise OperationRefused(
            STANDING_GRANT_STATE,
            f"standing grant {grant_id} was revoked while it was being revoked; "
            "nothing was written",
        )
    return _published(_must_read(uow, grant_id, now), now)


CREATE_DECLARATION: Final = OperationDeclaration(
    name=STANDING_GRANT_CREATE,
    safety_class=SafetyClass.MUTATE,
    roles=_GRANT_ROLES,
    input_model=StandingGrantCreate,
    output=StandingGrantRecord,
    # Each grant is its own act; nothing makes a repeat the same row, which is what
    # ``NATURAL`` would claim.
    idempotency=Idempotency.NONE,
    # ``MUTATE``, so an ``AuditSpec`` is required. ``subject_field=None`` because the
    # spec names an *input-model field carrying a RecordRef* and this input names an
    # account id and a list of operation names, neither of which is one.
    audit=AuditSpec(subject_field=None),
)

REVOKE_DECLARATION: Final = OperationDeclaration(
    name=STANDING_GRANT_REVOKE,
    safety_class=SafetyClass.MUTATE,
    roles=_GRANT_ROLES,
    input_model=StandingGrantRef,
    output=StandingGrantRecord,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)

GRANT_OPERATIONS: Final[tuple[tuple[OperationDeclaration, Handler], ...]] = (
    (CREATE_DECLARATION, create_handler),
    (REVOKE_DECLARATION, revoke_handler),
)
"""The pair ``register_core_operations()`` registers, mirroring
``approvals/operations.py``'s own ``APPROVAL_OPERATIONS`` tuple so a reader who knows
one recognises the other."""
