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
   guards run, the original handler runs exactly once, the approval becomes
   ``executed``, the held operation record is terminalised and the gated operation's
   own audit row is written. The approval's transition and the effect commit together
   or neither does. A guard that refuses replaces all of that with two writes — the
   approval ``refused``, the held record ``failed`` with the guard's code — which
   commit for the same reason: they are what the refusal *left behind*, and a rollback
   would leave nothing behind at all.

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
from rheo_core.approvals.guards import GuardRefusal, core_guards_for, run_guards
from rheo_core.audit import AUDIT_SUCCEEDED
from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import context_for_approved_execution
from rheo_core.operations.records import (
    AUDIENCE_NONE,
    HANDLER_RETURNED,
    finish_held_failed,
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
"""The code an execution guard's refusal is recorded under, naming the guard in its
text.

**It is an ``error_code`` on the held operation record, not a state a caller is
handed.** ``confirmation-and-safety.md`` § Execution guards fixes what a refused guard
leaves behind — the approval ``refused``, "the operation ``failed`` with the guard's
code", the effect untouched — and all three of those are *durable*. They can only be
durable if the transaction that discovers the refusal commits, so
:func:`execute_approved` records them and returns rather than raising: a raise would
reach ``dispatch``'s rollback and leave the approval ``pending`` and the record still
waiting, which is the state R3 item 4 exists to end. What the caller of
``core.approval.approve`` gets instead is its published
:class:`~rheo_core.approvals.operations.ApprovalRecord`, reading ``state = 'refused'``
— the approve operation genuinely succeeded at deciding and recording, and the
decision it recorded is that the effect may not run."""

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

    **Two refusals, and neither writes anything.** The payload snapshot exceeding
    ``approvals.max_payload_bytes`` is answered before anything is opened. The
    owned-delete pre-mint check is answered inside the unit of work but before the
    mint, so the ``with`` exit rolls its read back and the call still leaves no
    ``core.operation`` row and no ``core.approval`` row behind.

    **Why a deletion check is reached from here at all**, when this function is
    otherwise indifferent to which operation it is holding. ``core.record.delete`` is
    ``DESTRUCTIVE``, so ``dispatch()`` never enters its handler on the hold path — the
    class branch answers ``approval_required`` without running anything — and the
    ratified contract requires the owned-delete authorizer to run *before approval work
    is minted*, so that a reference core will not delete never creates an approval for
    somebody to release. There is no earlier hook: ``authorize`` is per-role and
    per-module, not per-reference, and input validation reaches no database. This
    function is the one that mints, so it is where "before anything is minted" is a
    place rather than a wish. The hook answers ``None`` for every other declaration in
    the tree — it is one implementation behind one name, not a general pre-approval
    mechanism — and is imported inside the function for the direction the module
    docstring states.
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
        # Deferred: ``rheo_core.deletion.operations`` imports ``rheo_core.operations``,
        # whose package ``__init__`` imports ``core_ops``, which reaches back into this
        # package. The same deferral, for the same reason, as the audit writer below.
        from rheo_core.deletion.operations import (  # deferred: see the docstring
            pre_mint_refusal,
        )

        pre_mint = pre_mint_refusal(ctx, uow, declaration, model_input)
        if pre_mint is not None:
            return pre_mint
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
            # The purpose the *held caller* was bound to, which is what
            # ``context_for_approved_execution`` rebuilds against. Null when the
            # caller was unbound (a web session, an unbound token, an operator), and
            # that null is load-bearing rather than a gap: it is the difference
            # between "no purpose gate applied to this call" and "some purpose did".
            purpose=(
                None
                if ctx.principal.bound_purpose is None
                else ctx.principal.bound_purpose.value
            ),
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


def _record_guard_refusal(
    uow: UnitOfWork,
    *,
    approval: records.ApprovalRow,
    refusal: GuardRefusal,
    now: datetime,
) -> GuardRefusal:
    """Record a guard's refusal durably, in the transaction that found it.

    Two writes and no effect: the approval leaves ``approved`` for ``refused``, and the
    held operation record leaves ``approval_required`` for ``failed`` carrying
    :data:`GUARD_REFUSED` and the guard's own name. The handler has not run — the
    guards are checked before it — so "the fixture untouched" needs nothing undone.

    **Both writes land in the caller's transaction and are meant to commit**, which is
    why this returns the refusal instead of raising it. See :data:`GUARD_REFUSED`. A
    second connection could not do this job: the approve transaction holds the
    approval's row lock from its own ``approved`` write, so an independent writer would
    block on it while it waited for that same transaction to end.

    Neither rowcount is checked, and that is deliberate rather than an omission. The
    ``approved`` transition was made in this transaction moments ago and no other
    connection can see the row to move it, so a zero here is unreachable; raising on
    one would trade a recorded refusal for a rollback, which is the exact outcome this
    function exists to prevent. The states are read back by the operation's published
    record either way, so a write that somehow did not apply is visible to the caller
    rather than asserted here.
    """
    records.mark_guard_refused(uow.connection, approval_id=approval.id)
    finish_held_failed(
        uow.connection,
        operation_id=approval.operation_id,
        now=now,
        error_code=GUARD_REFUSED,
        error_text=f"{refusal.guard}: {refusal.detail}",
    )
    return refusal


def execute_approved(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    *,
    approval: records.ApprovalRow,
    now: datetime,
    registry: OperationRegistry = REGISTRY,
) -> BaseModel | GuardRefusal:
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

    **The gated handler runs as the original held caller, never as the approver.**
    ``ctx`` is the approving actor's context — this function is called from inside
    ``core.approval.approve``'s handler — and
    :func:`~rheo_core.boundary.factories.context_for_approved_execution` rebuilds the
    context the held call was made under, from the approval's own actor fields and the
    held ``core.operation`` row's audience and entry. The guards, the handler **and the
    gated operation's own audit row** all take *that* context. Before 1a1 they received
    ``ctx``, which meant a member's held call executed with an owner's role, operation
    set and audience the moment an owner released it; the audit row stayed on ``ctx``
    one revision longer, which meant a correctly-executed gated operation still
    recorded the approver as its actor. A rebuild that *refuses* does not short-circuit
    the guards — see the comment at its call site for why a durable guard refusal has
    to win over the rollback a raise would cause.

    **The approving actor is not lost by that change.** ``core.approval.approve`` is
    itself a ``MUTATE`` operation dispatched by the approver, so ``dispatch()`` writes
    its own ``core.audit_record`` row under the approver's context in this same
    transaction, and ``core.approval.approved_by_kind``/``approved_by_id``/
    ``approved_entry`` record the release on the approval row. Two rows, two actors,
    each naming the act it belongs to.

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
    - ``output_invalid`` — the handler returned something that is not its declared
      output type; the same check ``dispatch()`` makes on an ordinary call, made here
      because this call does not go back through ``dispatch()``.
    - ``audit_sink_missing`` — the gated operation's module has no installed sink, so
      its row could not be written. The hold refused the same way before minting
      anything, so reaching it here means the process's wiring changed in between.

    **A guard refusal is the one outcome that does not raise.** It answers with the
    :class:`~rheo_core.approvals.guards.GuardRefusal` after recording the approval
    ``refused`` and the held record ``failed``, because those two states are required
    to be durable and a raise would roll them back with everything else. See
    :data:`GUARD_REFUSED` and :func:`_record_guard_refusal`. No audit row is written
    for the refused execution: the gated operation never ran, and the audit row for
    the held call — outcome ``refused``, written when the call was held — is already
    the row that says this operation did not happen. The operation record carries the
    guard's name for the reader who wants to know *why*.
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
    # The original held caller, rebuilt and re-verified, before the guards and before
    # the effect. ``ctx`` is the *approver's* and from here on governs nothing.
    held_caller = context_for_approved_execution(ctx, approval)
    # After the binding check, before the effect: the position the ratified document
    # fixes for the guards (R3 item 4). ``now`` is the same instant the binding was
    # just judged against, so a guard and the window cannot disagree about when this
    # execution happened. The set is derived from this declaration rather than taken
    # whole, because ``RecordStateGuard`` attaches only to an operation whose input
    # names a subject.
    #
    # **The guards run even when the rebuild refused**, under the approver's context
    # as they did before 1a1, and that fallback is load-bearing rather than defensive.
    # The commonest reason a rebuild refuses is ``membership_missing`` — the gated
    # actor's ``control.membership`` row is gone — which is precisely the case
    # ``ActorPermissionGuard`` exists to answer, and the ratified answer to it is
    # *durable*: the approval ``refused`` and the held record ``failed``, committed, so
    # the approval is spent rather than immediately re-approvable (R3 item 4, and
    # ``GUARD_REFUSED``'s own docstring). Raising the rebuild's refusal straight out
    # would roll both writes back and leave the approval ``pending``, which is the one
    # outcome that requirement exists to prevent. So the guards get first refusal, and
    # only a rebuild failure they did *not* catch — a tampered ``purpose`` column, a
    # missing provenance row — reaches the raise below, where a rollback is the right
    # answer because nothing was decided.
    refusal = run_guards(
        ctx if isinstance(held_caller, Refusal) else held_caller,
        uow,
        approval=approval,
        model_input=model_input,
        now=now,
        guards=core_guards_for(declaration),
    )
    if refusal is not None:
        return _record_guard_refusal(uow, approval=approval, refusal=refusal, now=now)
    if isinstance(held_caller, Refusal):
        raise OperationRefused(held_caller.state, str(held_caller))
    output = operation.handler(
        held_caller,
        HandlerUnitOfWork(
            uow,
            operation_id=approval.operation_id,
            # Carried through from the view ``core.approval.approve``'s own dispatch
            # built, so a gated handler that publishes reaches the composition root's
            # one registry rather than finding ``None`` and refusing
            # ``consumers_missing``. ``dispatch()`` threads it into the approve
            # operation's view; without this line the thread stops one frame short of
            # the operation that actually has an event to publish.
            consumers=uow.consumers if isinstance(uow, HandlerUnitOfWork) else None,
            # Carried through for the same reason and in the same shape, and
            # **carried through only**: this function never produces one. No approval
            # reaches here from the worker's leased-job path today, so the value is
            # ``None`` in every execution release one can run — but "a capability
            # travels with the view rather than being rebuilt at each frame" is the
            # rule that has to hold everywhere, because the frame that rebuilds one is
            # the frame that could invent one.
            scheduled_execution=(
                uow.scheduled_execution if isinstance(uow, HandlerUnitOfWork) else None
            ),
        ),
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
        # **The held caller, not ``ctx``.** The sink reads the workspace, the actor and
        # the entry off the context it is given, so passing the approver's here wrote a
        # ``succeeded`` row for the gated operation naming the person who *released*
        # the call rather than the person who made it — and the audit row is the
        # tree's record of who did what. Since 1a1 the guards and the handler both run
        # as ``held_caller``; this is the third reader that was still on ``ctx``. It is
        # narrowed to a ``WorkspaceContext`` by the raise a few lines up, so there is
        # no refusal case to handle here.
        held_caller,
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
