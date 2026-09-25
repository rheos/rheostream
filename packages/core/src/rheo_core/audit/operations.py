"""``core.audit.list``'s models and handler, beside the repository they read.

The declaration and the registration live in ``operations/core_ops.py``; the models
and the handler live **here**. That is the split ``work/operations.py`` established for
``core.work.failures`` and ``operations/operation_ops.py`` repeats for the three
``core.operation`` operations, and here it is an import-direction constraint rather
than a preference: ``rheo_core.operations``'s own ``__init__.py`` imports ``core_ops``
as its first statement, so **nothing in this module may import from
``rheo_core.operations``**. The reverse import would close a real cycle, and this
package is on the far side of one that already runs the other way —
``operations/dispatch.py`` imports ``rheo_core.audit``.

Nothing here needs one. ``dispatch()`` is what authorizes the call, validates the
input, seals the unit of work and turns a refusal into an outcome, and this read
handler raises nothing on its happy path.

**This is the supported read of the audit record** (AC 26 / criterion 14's third
sentence). A caller — and every test in this run — reads audit rows through this
operation and never by querying ``core.audit_record``, which is what makes "readable
through a supported operation rather than a raw table read" a property of the system
rather than of a convention.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from rheo_contracts import ActorKind, Role, WorkspaceContext

from rheo_core.audit.records import AuditRow, list_audit_records
from rheo_core.audit.tool_telemetry import (
    ToolTelemetryRow,
    list_tool_telemetry,
    telemetry_horizon,
)
from rheo_core.storage.backend import UnitOfWork

if TYPE_CHECKING:  # pragma: no cover - see ``_deletions`` on why this is deferred
    from rheo_core.deletion.records import DeletionRecordRow

AUDIT_LIST: Final = "core.audit.list"

TELEMETRY_ROLES: Final = frozenset({Role.OWNER, Role.OPERATOR})
"""Who may read the tool-telemetry collection.

The same pair ``AUDIT_LIST_DECLARATION`` already restricts the whole operation to
(``operations/core_ops.py``), which ``dispatch`` enforces before this handler runs —
so a member or a service token is refused ``role_not_permitted`` and never reaches
the flag. This second gate is at the point of disclosure rather than at the door, and
it is here on purpose: § A11 makes owner/operator-only access a property of the
telemetry collection itself, and a later run widening the *operation's* roles (to let
a member read their own audit trail, say) must not silently widen this with it."""

DELETION_ROLES: Final = TELEMETRY_ROLES
"""Who may read the deletion-evidence collection (§ A8).

The same owner/operator pair, and it is an alias rather than a second literal so the
two cannot drift into disagreement while both claim to be "the owner/operator-only
collections". § A8 names this collection in the same breath as the ledger's exact
counters — "exact counters remain on the content-free deletion ledger and in the
owner/operator-only ``audit.list`` opt-in ``deletions`` collection" — and those counters
are the closure size every other surface deliberately withholds, so the gate is at the
point of disclosure here for the reason :data:`TELEMETRY_ROLES` gives: an operation
whose own roles widen later must not widen this with them."""

MODEL_HELD_TOKEN_KINDS: Final = frozenset({"mcp", "runtime"})
"""Token kinds a model holds, which may not open either opt-in collection (#127).

An ``mcp`` token is made for an MCP client and a ``runtime`` token is handed to one
model run; both put this operation within a model's reach, the first on the ``api``
surface as well as the ``mcp`` one, since the ``api`` surface accepts ``mcp`` tokens.
``audit_list``, the tool, declares ``limit`` only, but a narrowed tool schema binds
the tool call and nothing else, so the rule lives here, in the operation, where every
surface meets it. A ``cli`` token and a person's session keep both opt-ins."""

# See ``rheo_core.operations.core_ops`` for why ``ignore`` (the default) is stated.
_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")


class AuditListInput(BaseModel):
    """How many of the most recent audit records to return.

    ``limit`` is bounded rather than a bare ``int``, for the reason
    ``work/operations.py``'s ``FailureListInput`` gives: a bare ``int`` lets ``-1``
    reach the ``SELECT``, where Postgres raises *LIMIT must not be negative* and
    ``dispatch`` folds a driver class name into ``handler_failed``, when it is an
    input defect that belongs in the ``input_invalid`` refusal pydantic already
    produces. Nothing prunes this table in release one, so an unbounded ``limit``
    would be a read that grows for the life of the deployment.
    """

    model_config = _IGNORE_EXTRA

    limit: int = Field(default=50, ge=1, le=500)

    include_tool_telemetry: bool = False
    """Opt in to § A11's bounded tool-telemetry collection.

    Default ``False``, so the response shape every existing caller reads is unchanged
    and the extra query is not run for a caller that did not ask for it. **It shares
    ``limit`` above rather than declaring a second one**: two independent bounds on
    one read is two things to reason about and two ways to get a page wrong, and the
    existing bound is already the right shape — "how many of the most recent rows",
    asked of both collections at once.
    """

    include_deletions: bool = False
    """Opt in to § A8's owner/operator-only deletion-evidence collection.

    The same shape as :attr:`include_tool_telemetry` above, down to sharing ``limit``,
    and for the same reasons: an existing caller's response is unchanged, the extra
    query does not run for a caller that did not ask, and one bound covers every
    collection on the read.

    The two flags are independent rather than one "everything" switch. They disclose
    different things — what a tool did, and what a deletion removed — and § A11 and
    § A8 make each its own owner/operator decision, so a caller that wants the ledger
    should not have to ask for telemetry to get it.
    """


class AuditRecord(BaseModel):
    """One audit row as this operation publishes it.

    :class:`~rheo_core.audit.records.AuditRow` field for field, with that row's ``id``
    published as ``audit_id`` — the renaming ``FailedJob`` and ``OperationRecord``
    already make for the same reason — and with ``request_digest`` published as its
    lowercase hex. The column is ``bytea``; a generated TypeScript client has no
    natural type for raw bytes, and hex is what a person comparing two digests by eye
    can actually compare.
    """

    model_config = ConfigDict(frozen=True)

    audit_id: UUID
    occurred_at: datetime
    actor_kind: str
    actor_id: UUID | None
    entry: str
    operation_name: str
    safety_class: str
    operation_id: UUID | None
    subject_ref: str | None
    request_digest: str
    outcome: str


class ToolTelemetryRecord(BaseModel):
    """One ``core.tool_telemetry`` row as this operation publishes it.

    :class:`~rheo_core.audit.tool_telemetry.ToolTelemetryRow` field for field, with
    that row's ``id`` published as ``telemetry_id`` — the renaming ``AuditRecord``
    above already makes, for the same reason.

    Every field is metadata by construction (``audit/telemetry_tables.py``): there is
    no query text, no argument value, and no exception message in the table, so there
    is none to withhold here. ``query_length`` is a measurement of an input, never the
    input.
    """

    model_config = ConfigDict(frozen=True)

    telemetry_id: UUID
    occurred_at: datetime
    tool_name: str
    safety_class: str
    mode: str
    result_count: int
    duration_ms: int
    outcome: str
    query_length: int | None
    argument_names: list[str]


class DeletionRecord(BaseModel):
    """One ``deletion_record`` row as this operation publishes it (§ A8).

    :class:`~rheo_core.deletion.records.DeletionRecordRow` field for field, with that
    row's ``id`` published as ``deletion_id`` — the renaming :class:`AuditRecord` and
    :class:`ToolTelemetryRecord` already make — and the qualified ``record_type`` and
    ``record_id`` republished as the canonical reference an operator actually reads,
    which is how every other surface names a record.

    **Every field here is content-free by construction**, which is the table's own
    design rather than a filter applied at this boundary: there is no title, body,
    payload or json column on the row to withhold. The four counters are the exact
    closure sizes § A8 keeps off the deletion *result* — a caller of
    ``core.record.delete`` receives only ``{deletion_ref}`` — and this collection is
    the one place they are published, to an owner or an operator who asked for them.
    """

    model_config = ConfigDict(frozen=True)

    deletion_id: UUID
    deleted_at: datetime
    record_ref: str
    actor_kind: str
    actor_id: UUID | None
    approval_id: UUID | None
    participants: list[str]
    cancelled_job_count: int
    cancelled_action_count: int
    removed_export_count: int
    invalidated_memory_count: int
    cause: str
    retained_successor_ref: str | None


class AuditList(BaseModel):
    """The workspace's most recent audit records, newest first.

    ``records`` is a named collection field rather than a bare list, mirroring
    ``FailureList`` and ``OperationList``: a later run adding a second collection
    beside it is then an additive change a reader may ignore rather than a change to
    the response's own type. ``tool_telemetry`` is that later run, and it is exactly
    that additive change.
    """

    model_config = ConfigDict(frozen=True)

    records: list[AuditRecord]

    tool_telemetry: list[ToolTelemetryRecord] | None = None
    """§ A11's collection, present only when it was asked for and allowed.

    ``None`` and ``[]`` are different answers and the distinction is deliberate:
    ``None`` means this read did not collect telemetry — the caller did not opt in, or
    is not owner/operator — while ``[]`` means it did and there is none unexpired to
    show. A default of ``[]`` would tell a member their workspace has no tool activity.
    """

    deletions: list[DeletionRecord] | None = None
    """§ A8's collection, present only when it was asked for and allowed.

    The same ``None``-versus-``[]`` distinction :attr:`tool_telemetry` draws, and it
    matters more here: ``[]`` from a read that was not allowed to collect would tell a
    member that nothing in their workspace has ever been deleted, which is precisely
    the fact the content-free ledger is careful not to publish.
    """


def _published(row: AuditRow) -> AuditRecord:
    return AuditRecord(
        audit_id=row.id,
        occurred_at=row.occurred_at,
        actor_kind=row.actor_kind,
        actor_id=row.actor_id,
        entry=row.entry,
        operation_name=row.operation_name,
        safety_class=row.safety_class,
        operation_id=row.operation_id,
        subject_ref=row.subject_ref,
        request_digest=row.request_digest.hex(),
        outcome=row.outcome,
    )


def _published_telemetry(row: ToolTelemetryRow) -> ToolTelemetryRecord:
    return ToolTelemetryRecord(
        telemetry_id=row.id,
        occurred_at=row.occurred_at,
        tool_name=row.tool_name,
        safety_class=row.safety_class,
        mode=row.mode,
        result_count=row.result_count,
        duration_ms=row.duration_ms,
        outcome=row.outcome,
        query_length=row.query_length,
        argument_names=list(row.argument_names),
    )


def _published_deletion(row: "DeletionRecordRow") -> DeletionRecord:
    return DeletionRecord(
        deletion_id=row.id,
        deleted_at=row.deleted_at,
        record_ref=f"{row.record_type}:{row.record_id}",
        actor_kind=row.actor_kind,
        actor_id=row.actor_id,
        approval_id=row.approval_id,
        participants=list(row.participants),
        cancelled_job_count=row.cancelled_job_count,
        cancelled_action_count=row.cancelled_action_count,
        removed_export_count=row.removed_export_count,
        invalidated_memory_count=row.invalidated_memory_count,
        cause=row.cause,
        retained_successor_ref=row.retained_successor_ref,
    )


def _deletions(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: AuditListInput
) -> list[DeletionRecord] | None:
    """§ A8's collection, or ``None`` when this read does not collect it.

    No age cut, unlike :func:`_telemetry`: the ledger is the durable proof that a
    deletion happened and nothing expires it, so "the most recent ``limit``" is the
    whole of the bound.

    **The repository is imported here rather than at module level**, and the reason
    is the structural one this module's own docstring states: nothing in this package
    may import ``rheo_core.operations``. ``rheo_core.deletion``'s package ``__init__``
    imports its registry, which imports that package's refusals, which runs
    ``operations/__init__`` — and that imports ``core_ops``, which imports this module.
    A module-level import here closes that loop and every name below is unbound by the
    time ``core_ops`` wants one. Deferred to call time, when the chain has finished
    loading.
    """
    from rheo_core.deletion.records import list_deletion_records

    if not model_input.include_deletions or ctx.role not in DELETION_ROLES:
        return None
    return [
        _published_deletion(row)
        for row in list_deletion_records(uow.connection, limit=model_input.limit)
    ]


def _telemetry(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: AuditListInput
) -> list[ToolTelemetryRecord] | None:
    """§ A11's collection, or ``None`` when this read does not collect it.

    The age cut is computed here, from this workspace's own
    ``telemetry.tool_retention_days`` read on this transaction's connection, so an
    expired row is excluded the moment it expires rather than the next time the daily
    sweep runs — § A11's "audit reads exclude expired telemetry immediately regardless
    of when the daily sweep last ran".
    """
    if not model_input.include_tool_telemetry or ctx.role not in TELEMETRY_ROLES:
        return None
    not_before = telemetry_horizon(ctx, uow.connection, datetime.now(UTC))
    return [
        _published_telemetry(row)
        for row in list_tool_telemetry(
            uow.connection, limit=model_input.limit, not_before=not_before
        )
    ]


def _refuse_model_held_opt_ins(
    ctx: WorkspaceContext, model_input: AuditListInput
) -> None:
    """Refuse ``operation_not_permitted`` when a model-held token asks for an opt-in.

    The context names a token actor by id and not by kind, so the kind is read from
    the token's control-plane row, and only when an opt-in was actually asked for: a
    plain listing costs no extra read. A token row that is gone by now (a run's token
    is deleted when the run ends) is treated as model-held, so the rule fails closed.

    Refused rather than answered with ``None``: ``None`` already means "not asked or
    not owner/operator", and a caller that is an owner's model should be told the
    opt-in is closed to it rather than be shown an answer that looks like the other.

    Imports are deferred for the cycle this module's docstring describes.
    """
    if not (model_input.include_tool_telemetry or model_input.include_deletions):
        return
    if ctx.actor.kind is not ActorKind.TOKEN:
        return
    from rheo_core.operations.refusals import (  # deferred, see module docstring
        OPERATION_NOT_PERMITTED,
        OperationRefused,
    )
    from rheo_core.storage.control_plane import get_access_token
    from rheo_core.storage.postgres import get_backend

    kind: str | None = None
    if ctx.actor.id is not None:
        with get_backend().control_engine.connect() as connection:
            row = get_access_token(connection, ctx.actor.id)
        kind = None if row is None else row.kind
    if kind is None or kind in MODEL_HELD_TOKEN_KINDS:
        raise OperationRefused(
            OPERATION_NOT_PERMITTED,
            "include_tool_telemetry and include_deletions are not available to an "
            f"{kind or 'unknown'}-kind token; a model may read the audit records only",
        )


def audit_list_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: AuditListInput
) -> AuditList:
    """The most recent audit records of the context's own workspace.

    Named ``audit_list_handler`` rather than ``list_handler`` because ``core_ops.py``
    already imports a ``list_handler`` from ``operation_ops.py``, and two handlers
    under one name in one importer is a rename waiting to bind the wrong function.

    The workspace is the context's: ``uow.connection`` is already routed to it, so no
    input field names one and none can.

    A model-held token asking for either opt-in is refused before anything is read
    (:func:`_refuse_model_held_opt_ins`).
    """
    _refuse_model_held_opt_ins(ctx, model_input)
    return AuditList(
        records=[
            _published(row)
            for row in list_audit_records(uow.connection, limit=model_input.limit)
        ],
        tool_telemetry=_telemetry(ctx, uow, model_input),
        deletions=_deletions(ctx, uow, model_input),
    )
