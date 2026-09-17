"""The dispatcher's upper-class path: hold a gated call, and execute an approved one.

Two functions, and between them they are the whole of what
``docs/architecture/confirmation-and-safety.md`` § Flow describes:

1. :func:`hold_for_approval` — a ``DESTRUCTIVE``, ``EXTERNAL`` or ``FINANCIAL`` call
   arrives. One transaction writes the ``core.operation`` record, moves it to
   ``approval_required``, writes the ``core.approval`` row ``pending`` with its payload
   snapshot, and writes the call's audit row. The handler does not run and nothing is
   recorded at any destination. ``dispatch()`` answers with both ids.
2. :func:`execute_approved` — ``core.approval.approve`` has moved the row to
   ``approved`` in its own transaction and calls this **inside that same
   transaction**: the binding tuple is rechecked against the payload about to run, the
   guard seam runs, the original handler runs exactly once, the approval becomes
   ``executed``, the held operation record is terminalised and the gated operation's
   own audit row is written. The approval's transition and the effect commit together
   or neither does.

**Everything from ``rheo_core.operations`` is imported at module level here, by
submodule and never through the package.** ``rheo_core.refs.resolver`` — which this
module needs for a subject's revision — already imports ``rheo_core.operations.
registry`` that way, so the dependency exists whatever this file does. The direction
that must **not** exist is the other one: ``operations/dispatch.py`` imports
:func:`hold_for_approval` inside the branch that calls it, and
``operations/core_ops.py`` imports this package's declarations inside
``register_core_operations()``, exactly as it already defers ``rheo_core.tokens.
issue``. A module-level import either way round would close a real cycle, and a
``from rheo_core.operations import X`` here would fail for a different reason: the
package's own ``__init__`` imports ``core_ops`` as its first statement, so the package
object is only partially initialised while any of this is being imported.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ValidationError
from rheo_contracts import (
    OperationDeclaration,
    RecordRef,
    WorkspaceContext,
)

from rheo_core.approvals import records
from rheo_core.approvals.binding import (
    INVALID_APPROVAL,
    binding_detail,
    executable_payload,
    payload_bytes,
    payload_digest,
)
from rheo_core.approvals.guards import run_guards
from rheo_core.audit import AUDIT_SUCCEEDED
from rheo_core.boundary.context import Refusal
from rheo_core.operations.records import (
    AUDIENCE_NONE,
    HANDLER_RETURNED,
    finish_held_succeeded,
    mark_approval_required,
    mint,
)
from rheo_core.operations.refusals import (
    OPERATION_UNKNOWN,
    OUTPUT_INVALID,
    OperationRefused,
)
from rheo_core.operations.registry import REGISTRY, OperationRegistry
from rheo_core.refs.resolver import Unavailable, resolve_in
from rheo_core.settings import resolve as resolve_settings
from rheo_core.settings.storage_source import PostgresOverrideSource
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.routing import open_unit_of_work

PAYLOAD_TOO_LARGE: Final = "approval_payload_too_large"
"""The refusal for an input whose snapshot would exceed
``approvals.max_payload_bytes``.

Refused rather than truncated or stored anyway: the snapshot is what a person reads
at the point of confirmation, and an approval whose stored payload is not the payload
would be a confirmation of something else."""

GUARD_REFUSED: Final = "guard_refused"
"""The refusal an execution guard produces, naming the guard in its detail.

Unreachable in this chunk — :data:`~rheo_core.approvals.guards.CORE_GUARDS` is empty —
and declared here anyway because it is the seam's own contract: the chunk that writes
the guards writes no new refusal vocabulary, and it is also what writes the approval
``refused`` with the guard named, which this chunk deliberately does not do for a
refusal that cannot happen."""

AuditWriter = Callable[[UnitOfWork, UUID], None]
"""What :func:`hold_for_approval` is handed to write the held call's audit row.

A callback rather than the sink itself, because the dispatcher has already resolved
the sink, the request digest and the subject for this call and holds them in one
object; handing over a closure keeps the six-keyword protocol spelled at exactly one
call site and keeps the row inside this function's transaction."""


@dataclass(frozen=True, slots=True)
class HeldCall:
    """The two ids a held call answers with: the approval to act on, and the
    operation record that is waiting for it."""

    approval_id: UUID
    operation_id: UUID


def window_seconds(ctx: WorkspaceContext) -> int:
    """The execution window's length for this workspace, in seconds.

    ``approvals.default_window_seconds`` (900), clamped to
    ``approvals.max_window_seconds`` (86400): the maximum is a bound on the setting
    and not only on a caller, so a deployment that raises the default past the
    maximum gets the maximum rather than its own mistake. No caller chooses a length
    in release one — no operation takes one as an input — so the default is the
    length every approval gets.

    **Resolved with the workspace's own override rows, not from the deployment layer
    alone**, and that is what makes ``approvals.max_window_seconds``'s ``min`` floor
    real: the key is workspace-scope, so a workspace may shorten the longest window
    its own approvals can carry and can never lengthen it. ``resolve(workspace_id=…,
    source=PostgresOverrideSource())`` is exactly the call ``tokens/issue.py`` makes
    for ``identity.token_max_days.*``, the other floored key in the tree — a floored
    key read from the deployment layer would be a floor nothing applies.
    """
    settings = resolve_settings(
        workspace_id=ctx.workspace_id, source=PostgresOverrideSource()
    )
    return min(
        settings.get_int("approvals.default_window_seconds"),
        settings.get_int("approvals.max_window_seconds"),
    )


def _subject_revision(
    ctx: WorkspaceContext, uow: UnitOfWork, subject_ref: RecordRef | None
) -> int | None:
    """The subject's ``RecordHead.revision`` at the moment the approval is created.

    ``None`` when the call names no subject, and ``None`` when the subject does not
    resolve — a reference whose module is disabled, whose type has no resolver, or
    whose record is already gone. Not a refusal: what an unresolvable subject means
    for an upper-class call is ``RecordStateGuard``'s question at execution, and
    answering it here would put the guard's policy in the record that the guard is
    supposed to read.
    """
    if subject_ref is None:
        return None
    head = resolve_in(subject_ref, ctx, uow)
    return None if isinstance(head, Unavailable) else head.revision


def hold_for_approval(
    ctx: WorkspaceContext,
    *,
    declaration: OperationDeclaration,
    model_input: BaseModel,
    subject_ref: RecordRef | None,
    now: datetime,
    record_audit: AuditWriter,
) -> HeldCall | Refusal:
    """Hold this call: write the approval, the record and the audit row; run nothing.

    **One transaction, and it has to be.** The operation record says
    ``approval_required`` and names an approval; the approval says ``pending`` and
    names the record. A crash between two transactions would leave one of those
    naming something that does not exist — a record waiting for an approval nobody
    can find, or an approval whose subject record was never written — and both are
    states no later read can repair.

    **The audit row is written here rather than by the caller afterwards** for the
    same reason: a held call is an audited event (its safety class is above ``READ``,
    so a row is owed), and a row written in a second transaction could survive a
    rollback of the hold or be lost by a crash that kept it.

    A refusal — the payload snapshot exceeding ``approvals.max_payload_bytes`` — is
    answered before anything is opened, so a refused hold writes nothing at all.
    """
    body = executable_payload(model_input)
    byte_length = payload_bytes(body)
    limit = resolve_settings().get_int("approvals.max_payload_bytes")
    if byte_length > limit:
        return Refusal(
            PAYLOAD_TOO_LARGE,
            f"the input of {declaration.name} is {byte_length} bytes canonically, "
            f"over the approvals.max_payload_bytes bound of {limit}; an approval "
            "stores the payload it confirms",
        )
    window_end = now + timedelta(seconds=window_seconds(ctx))
    with open_unit_of_work(ctx) as uow:
        operation_id = mint(
            uow.connection,
            name=declaration.name,
            safety_class=declaration.safety_class.value,
            actor_kind=ctx.actor.kind.value,
            actor_id=ctx.actor.id,
            entry=ctx.entry.value,
            audience_kind=(
                AUDIENCE_NONE if ctx.audience is None else ctx.audience.kind.value
            ),
            audience_id=None if ctx.audience is None else ctx.audience.id,
            now=now,
        )
        approval_id = records.create(
            uow.connection,
            actor_kind=ctx.actor.kind.value,
            actor_id=ctx.actor.id,
            operation_name=declaration.name,
            operation_id=operation_id,
            # Null in release one: nothing declares a destination. See
            # ``approvals/tables.py``'s own comment for why that is a recorded gap
            # rather than a missing write.
            destination_ref=None,
            subject_ref=None if subject_ref is None else subject_ref.format(),
            subject_revision=_subject_revision(ctx, uow, subject_ref),
            payload_digest=payload_digest(body),
            body=body,
            byte_length=byte_length,
            purpose=None,
            window_start=now,
            window_end=window_end,
        )
        if not mark_approval_required(
            uow.connection, operation_id=operation_id, approval_id=approval_id
        ):
            # Unreachable by construction: the record was minted ``pending`` three
            # statements ago in this transaction, and no other connection can see it
            # to move it. Raised rather than ignored because the alternative — a
            # committed ``pending`` record for a call that is about to answer
            # ``approval_required`` — is a record nothing would ever terminalise.
            raise RuntimeError(
                f"the operation record {operation_id} minted for {declaration.name} "
                "could not be moved to approval_required"
            )
        record_audit(uow, operation_id)
        uow.commit()
    return HeldCall(approval_id=approval_id, operation_id=operation_id)


def execute_approved(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    *,
    approval: records.ApprovalRow,
    now: datetime,
    registry: OperationRegistry = REGISTRY,
) -> BaseModel:
    """Run the operation this approval was minted for, exactly once, and record it.

    Called by ``core.approval.approve``'s handler inside that operation's own
    transaction, with ``approval`` already read back in state ``approved``. Every
    write below — the effect, the approval's ``executed``, the held record's terminal
    state, the gated operation's audit row — lands in that one transaction, so the
    approval cannot be marked executed by a call whose effect rolled back, and the
    effect cannot land under an approval that stayed ``approved``.

    **Exactly once is the database's answer, not this function's.**
    ``mark_approved`` took the row's lock in this transaction and is predicated on
    ``pending``; a second approver blocks there and then matches nothing.
    :func:`~rheo_core.approvals.records.mark_executed` is predicated on ``approved``
    for the same reason one layer in.

    **The registry it resolves against is the process-wide one**, because a handler
    receives ``(ctx, uow, input)`` and has no way to learn which registry dispatched
    it. Every real caller uses ``REGISTRY``; a call held through a *private* registry
    — which only a test builds — refuses ``operation_unknown`` here unless the same
    operation is registered process-wide too. Threading the registry through would
    mean widening the handler signature every registered operation shares, for a case
    that exists only in this suite.

    The refusals it raises, all as :class:`OperationRefused` so ``dispatch()`` rolls
    the whole transaction back and answers with the state:

    - ``operation_unknown`` — the approved operation is not registered in this
      process. A deployment that dropped a module between the hold and the approval.
    - ``invalid_approval`` — the snapshot is gone, no longer validates, or the
      binding tuple does not match what is about to run (criterion 60).
    - ``guard_refused`` — an execution guard refused. Unreachable in this chunk.
    - ``output_invalid`` — the handler returned something that is not its declared
      output type; the same check ``dispatch()`` makes on an ordinary call, made here
      because this call does not go back through ``dispatch()``.
    - ``audit_sink_missing`` — the gated operation's module has no installed sink, so
      its row could not be written. The hold refused the same way before minting
      anything, so reaching it here means the process's wiring changed in between.
    """
    operation = registry.lookup(approval.operation_name)
    if operation is None:
        raise OperationRefused(
            OPERATION_UNKNOWN,
            f"{approval.operation_name} is approved but not registered in this process",
        )
    declaration = operation.declaration
    body = records.payload_body(uow.connection, approval_id=approval.id)
    if body is None:
        raise OperationRefused(
            INVALID_APPROVAL,
            f"the payload snapshot of approval {approval.id} is no longer stored",
        )
    try:
        model_input = declaration.input_model.model_validate(body)
    except ValidationError:
        raise OperationRefused(
            INVALID_APPROVAL,
            f"the stored payload of approval {approval.id} no longer validates "
            f"against {declaration.input_model.__name__}",
        ) from None
    detail = binding_detail(
        approved_digest=approval.payload_digest,
        digest=payload_digest(executable_payload(model_input)),
        window_start=approval.window_start,
        window_end=approval.window_end,
        now=now,
    )
    if detail is not None:
        raise OperationRefused(INVALID_APPROVAL, detail)
    # After the binding check, before the effect: the position the ratified document
    # fixes for the guards (R3 item 4). Nothing is registered, so this passes — and
    # ``now`` is the same instant the binding was just judged against, so a guard and
    # the window cannot disagree about when this execution happened.
    refusal = run_guards(ctx, uow, approval=approval, model_input=model_input, now=now)
    if refusal is not None:
        raise OperationRefused(GUARD_REFUSED, f"{refusal.guard}: {refusal.detail}")
    output = operation.handler(
        ctx,
        HandlerUnitOfWork(uow, operation_id=approval.operation_id),
        model_input,
    )
    if not isinstance(output, declaration.output):
        raise OperationRefused(
            OUTPUT_INVALID,
            f"{declaration.name} returned {type(output).__name__}, not "
            f"{declaration.output.__name__}",
        )
    if not records.mark_executed(uow.connection, approval_id=approval.id, now=now):
        raise OperationRefused(
            INVALID_APPROVAL,
            f"approval {approval.id} left 'approved' while it was being executed",
        )
    if not finish_held_succeeded(
        uow.connection,
        operation_id=approval.operation_id,
        now=now,
        terminal_check_kind=HANDLER_RETURNED,
        terminal_check_at=now,
    ):
        raise OperationRefused(
            INVALID_APPROVAL,
            f"the operation record {approval.operation_id} left "
            "'approval_required' while its approval was being executed",
        )
    # Through the dispatcher's own writer rather than through the sink directly: the
    # six-keyword protocol is spelled at one call site in the tree and this is not a
    # second one. Deferred import for the direction the module docstring states.
    from rheo_core.operations.dispatch import (  # deferred: see module docstring
        AUDIT_SINK_MISSING,
        record_operation_audit,
    )

    written = record_operation_audit(
        ctx,
        uow,
        operation,
        # The digest of the input as it executed, which the binding check has just
        # proved is the digest the approval carries. One value rather than a second
        # rendering, so the audit row and the approval name the same request.
        request_digest=approval.payload_digest,
        # The **verified** input, never ``approval.subject_ref``. That column is a
        # second stored copy of the subject and the digest does not cover it, so a
        # row tampered there alone would have produced a ``succeeded`` audit row
        # naming a record this handler never touched. ``record_operation_audit``
        # derives the subject from what actually ran.
        model_input=model_input,
        outcome=AUDIT_SUCCEEDED,
        operation_id=approval.operation_id,
    )
    if not written:
        raise OperationRefused(
            AUDIT_SINK_MISSING,
            f"no audit sink is installed for module {operation.module_id!r}, which "
            f"owns {declaration.name}; an approved operation does not run without "
            "one either",
        )
    return output
