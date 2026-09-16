"""``dispatch(ctx, name, payload) -> OperationOutcome``: authorize, validate, run in
one unit of work, commit.

The ``context_required`` check comes first, before any lookup: ``None`` or an object
that is not a ``WorkspaceContext`` is refused without the registry being consulted.
Then ``authorize``; then, for an operation above the read class, the audit sink of its
owning module (``audit_sink_missing`` when it has none); then the payload is validated
into the declaration's input model (``input_invalid``, naming the failing fields but
never echoing values); then, for a ``long_running`` declaration and only for one, the
operation record is minted and committed on its own; then **one** work ``UnitOfWork``
is opened through ``route(ctx)``, the handler runs, the audit row is written, and the
unit of work commits. A handler that raises
:class:`OperationRefused` (or a
``StorageRefusal``) rolls back and yields that state; any other exception rolls back,
is logged with the context's ``request_id``, and yields ``failed`` with the fixed
code ``handler_failed`` and only the exception's class name as the text — never its
message, which for a driver error carries the statement and its parameters.

The handler receives a ``HandlerUnitOfWork`` — the same connection, with ``commit``,
``rollback`` and ``__enter__`` refused ``handler_may_not_commit``. Ending the one
transaction is this function's job.

**This function is the audit seam's one caller.** Issue #45 kept the ``AuditSink``
protocol and the module-keyed registry as substrate and left the call site — and what
a missing registration means — to 0c2; this is it. For every operation whose safety
class is not ``READ`` the sink installed for the *owning module* is resolved once, a
``None`` is refused ``audit_sink_missing`` before the handler runs, and exactly one
``core.audit_record`` row is written per dispatch: on the success path inside the
operation's own transaction, so a mutation and its record commit together or neither
does, and on every reachable non-success path in a short transaction of its own.
:func:`_audit_alone` and the table in its docstring say which paths are reachable and
which three are structurally outside the set.

**One operation record is minted here, and only for a ``long_running`` declaration.**
Run 0c2 gives ``OperationDeclaration.long_running`` its first reader: when it is set,
this function writes a ``pending`` ``core.operation`` row in its **own committed
transaction** before the work transaction is opened, so the id is readable through the
supported read while the work has not run; carries that id on
``OperationOutcome.operation_id`` and into the sealed view the handler receives; and
returns ``state = "pending"``. For every declaration that is not ``long_running``
nothing is minted and ``operation_id`` stays ``None``, exactly as before.

**This function never writes ``succeeded`` for an operation record.** The worker that
finishes the job carrying the id is the only thing that does, which is the whole of
the "one terminaliser" rule: a dispatcher that reported success would be claiming the
work was done at the moment it was merely scheduled.

**It does write ``failed``, on every exit below the mint that does not commit the work
transaction**, and it must. ``_mint_operation`` commits on its own, so a rollback of
the work transaction cannot undo it: without this, a handler that raised, refused,
returned the wrong type or queued nothing would leave a committed ``pending`` record
that no worker will ever see and nothing can ever move — while the caller was told the
dispatch failed. Six exits reach :func:`_fail_operation`: a ``StorageRefusal`` opening
the work transaction, ``OperationRefused`` or ``StorageRefusal`` out of the handler,
any other exception (a commit failure included, since the commit runs inside the same
``try``), an output that is not the declared type, and a ``long_running`` handler that
queued no job at all. The three refusals **above** the mint — ``context_required``,
``authorize`` and ``input_invalid`` — return before anything is written, so none of
them leaves a record behind.

**A ``long_running`` dispatch does not mark its workspace due, and the work therefore
waits for the reconcile floor.** ``work.jobs.enqueue`` calls ``mark_work_due`` after
committing, so a job queued that way is visible to the very next pass; a
``long_running`` handler instead calls ``enqueue_job`` inside *this* function's work
transaction, where it has no control-plane connection to write that mark with, and
this function has no post-commit hook to write it for it. So the job is discovered by
``run_one_pass`` only once ``record_visit``'s floor — ``work.due_reconcile_seconds``,
900 s by default — brings the workspace back round. **A long-running operation can sit
up to that long before it starts**, which is stated here rather than left to be
measured because ``tests/harness/registry.py``'s ``harness.note.schedule`` is the
pattern a future long-running handler is told to copy, and it inherits this. Closing it
means a post-commit ``mark_work_due`` here, the shape
``events/publish.py``'s ``mark_due_after_publish`` already anticipates for the outbox.

**No outbox event is enqueued here.** The audit half is no longer absent — see above —
but nothing in this function publishes an event.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ValidationError
from rheo_contracts import (
    OperationDeclaration,
    RecordRef,
    SafetyClass,
    WorkspaceContext,
)

from rheo_core.audit import (
    AUDIT_FAILED,
    AUDIT_REFUSED,
    AUDIT_SUCCEEDED,
    AuditOutcome,
    AuditSink,
    sink_for,
)
from rheo_core.boundary.context import CONTEXT_REQUIRED, Refusal
from rheo_core.operations.records import AUDIENCE_NONE, PENDING, finish_failed, mint
from rheo_core.operations.refusals import (
    FAILED,
    HANDLER_FAILED,
    INPUT_INVALID,
    OUTPUT_INVALID,
    SUCCEEDED,
    OperationRefused,
)
from rheo_core.operations.registry import (
    REGISTRY,
    OperationRegistry,
    RegisteredOperation,
)
from rheo_core.storage.backend import HandlerUnitOfWork, StorageRefusal, UnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.work.jobs import has_job_for_operation

logger = logging.getLogger("rheo_core.operations")

_DETAIL_LIMIT: Final = 2000

AUDIT_SINK_MISSING: Final = "audit_sink_missing"
"""The refusal state for a non-``READ`` operation whose owning module has no installed
audit sink (AC 25).

Declared here rather than in ``refusals.py`` because it is the *dispatcher's* refusal
and nothing else can raise it: the registry refuses a missing ``AuditSpec`` and startup
refuses a missing sink, so by the time a call reaches this state both earlier layers
have been bypassed — a registry built by hand, or a process that never ran
``check_audit_paths``."""


@dataclass(frozen=True, slots=True)
class OperationError:
    """What a non-success outcome carries: a state name and a showable text."""

    error_code: str
    error_text: str


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    """``state`` is ``succeeded`` with ``result``, ``pending`` with ``result`` and an
    ``operation_id`` for a ``long_running`` dispatch, or a refusal state / ``failed``
    with ``error``.

    ``operation_id`` is the ``core.operation`` row :func:`dispatch` minted for this
    call, and is ``None`` for every declaration that is not ``long_running`` — which
    is every operation registered in release one, so the API envelope still carries
    ``operation_id: null`` for all of them. It is the last field rather than the
    second so that nothing constructing this positionally had its arguments shift.
    """

    state: str
    result: BaseModel | None = None
    error: OperationError | None = None
    operation_id: UUID | None = None

    @property
    def ok(self) -> bool:
        return self.state == SUCCEEDED


def _refused(
    state: str, detail: str | None, *, operation_id: UUID | None = None
) -> OperationOutcome:
    """A refusal outcome, carrying the minted id when there is one.

    ``operation_id`` defaults to ``None`` because most refusals happen before
    anything is minted — the context check, ``authorize``, input validation. The
    refusals raised out of the handler happen after, and a caller that was handed an
    id must be able to look that record up whatever the outcome was.
    """
    return OperationOutcome(
        state,
        error=OperationError(state, detail or state),
        operation_id=operation_id,
    )


def _describe_validation_error(exc: ValidationError) -> str:
    """The failing locations and messages, never the input values."""
    parts: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)[:_DETAIL_LIMIT]


def _request_digest(payload: Mapping[str, object] | None) -> bytes:
    """The SHA-256 of the canonical request, for ``audit_record.request_digest``.

    Computed from the **raw payload**, once, before authorization, rather than from
    the validated input model — because three of the outcomes that need a row happen
    before validation has run or could run, and a digest that existed only on the
    validated paths would be the one column those rows could not fill.

    ``sort_keys`` makes two payloads that differ only in key order the same request;
    ``separators`` removes the whitespace that would otherwise make the same request
    two digests. ``default=str`` is what keeps this total: a payload reaching
    ``dispatch`` is a ``Mapping[str, object]`` and may hold a ``UUID`` or a
    ``datetime`` a caller never serialised, and a digest is not worth raising over.
    The digest stores no input — that is the whole point of the column, per
    ``intake-and-events.md`` § Audit — so a lossy rendering of an exotic value costs
    nothing a reader of the row could have had anyway.
    """
    canonical = json.dumps(
        {} if payload is None else dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(canonical.encode("utf-8")).digest()


def _subject_ref(
    declaration: OperationDeclaration, model_input: BaseModel
) -> RecordRef | None:
    """The record this call acts on, per the declaration's ``AuditSpec``.

    ``AuditSpec.subject_field`` names *an input-model field carrying a*
    :class:`~rheo_contracts.refs.RecordRef` (``manifest.py``), so a field holding
    anything else is not a subject reference and records none. **No release-one
    declaration names a subject field** — every ``AuditSpec`` in the tree is
    ``AuditSpec(subject_field=None)``, because none of the shipped mutate operations
    acts on a registered record type — so this function has no non-``None`` instance
    yet, and it is written as the narrowest thing that satisfies the contract rather
    than as a parser for the string form nothing produces.
    """
    spec = declaration.audit
    if spec is None or spec.subject_field is None:
        return None
    value = getattr(model_input, spec.subject_field, None)
    return value if isinstance(value, RecordRef) else None


@dataclass(frozen=True, slots=True)
class _AuditWrite:
    """Everything one dispatch's audit row needs that does not vary by outcome.

    Built once per dispatch and passed down, so the six keywords the protocol takes
    are spelled at one place rather than at each of the six exits that writes a row.
    Its existence is itself the "a row is written here" condition: it is ``None``
    exactly when no row is written, and every call site reads it that way.
    """

    operation: RegisteredOperation
    sink: AuditSink
    request_digest: bytes
    subject_ref: RecordRef | None

    def record(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        outcome: AuditOutcome,
        operation_id: UUID | None,
    ) -> None:
        """Write the row through ``uow``. Does not commit; the caller owns that."""
        declaration = self.operation.declaration
        self.sink.record(
            ctx,
            uow,
            operation=declaration.name,
            safety_class=declaration.safety_class,
            subject_ref=self.subject_ref,
            request_digest=self.request_digest,
            outcome=outcome,
            operation_id=operation_id,
        )


def _audit_write(
    operation: RegisteredOperation | None,
    *,
    request_digest: bytes,
    subject_ref: RecordRef | None = None,
) -> _AuditWrite | None:
    """What writes this dispatch's row, or ``None`` when no row is written.

    Three ways to get ``None``, all structural:

    - **no declaration** — ``operation_unknown``: there is no owning module to find a
      sink for and no safety class to record, and ``audit_record.safety_class`` is
      NOT NULL;
    - **a ``READ`` declaration** — audit is required only above ``READ``, and a read
      writes no row by design;
    - **a non-``READ`` declaration whose module has no installed sink** — nothing to
      write the row *with*. :func:`dispatch` refuses :data:`AUDIT_SINK_MISSING` rather
      than reaching this branch for every path after authorization, so the only way
      here is an authorization refusal for an operation that was never wired, which
      ``check_audit_paths`` makes impossible in a process that ran its startup. It is
      logged rather than silent, because a missing row is otherwise indistinguishable
      from a path that correctly writes none.
    """
    if operation is None:
        return None
    declaration = operation.declaration
    if declaration.safety_class is SafetyClass.READ:
        return None
    sink = sink_for(operation.module_id)
    if sink is None:
        logger.error(
            "audit_row_unwritable",
            extra={
                "operation": declaration.name,
                "module_id": operation.module_id,
            },
        )
        return None
    return _AuditWrite(operation, sink, request_digest, subject_ref)


def _audit_alone(
    ctx: WorkspaceContext,
    audit: _AuditWrite | None,
    *,
    outcome: AuditOutcome,
    operation_id: UUID | None = None,
) -> None:
    """Write one audit row in a short transaction of its own, and commit it.

    **The one mechanism behind both non-success shapes**, which read differently in
    the control flow and are the same write:

    - a refusal or failure raised *after* the work transaction was opened — the row
      cannot live in that transaction, because it is rolled back and
      ``audit_record.outcome`` admits ``failed`` and ``refused`` precisely for rows
      that outlive one;
    - a refusal reached *before* the work transaction was ever opened — the four
      reachable ones are ``operation_not_permitted``, ``module_disabled``,
      ``role_not_permitted`` (all from ``authorize``, all after the declaration
      resolved) and ``input_invalid``. There is no transaction to roll back from, so
      the fresh connection is opened for the row alone.

    ``_finish_alone`` in ``work/loop.py`` is the shape both copy, and for the same
    reason: one terminal write, its own transaction, committed unconditionally.

    **Its own unit of work, and it may fail.** ``open_unit_of_work`` can refuse — the
    workspace may have become unreachable between the refusal and now — and this
    function logs rather than raising, because ``dispatch()`` answers its caller with
    an outcome and never raises, exactly as ``_fail_operation`` one screen up decides
    for the operation record. The success path is not protected by any such log: it
    writes inside the operation's transaction, so a failure there fails the operation,
    which is the property AC 23 rests on.
    """
    if audit is None:
        return
    try:
        with open_unit_of_work(ctx) as uow:
            audit.record(ctx, uow, outcome=outcome, operation_id=operation_id)
            uow.commit()
    except Exception:
        logger.error(
            "audit_row_not_written",
            extra={
                "operation": audit.operation.name,
                "outcome": outcome,
                "workspace_id": str(ctx.workspace_id),
                "request_id": str(ctx.request_id),
            },
        )


def _mint_operation(ctx: WorkspaceContext, declaration: OperationDeclaration) -> UUID:
    """Write this call's ``core.operation`` row and **commit it**, returning its id.

    **Its own transaction, and it must be.** AC 15 is that the id is readable through
    the supported read *before the operation completes*; a row written inside the work
    transaction is invisible to every other connection until that transaction commits,
    which is after the work has run. So this opens a unit of work of its own, writes
    one ``pending`` row, commits, and closes — and only then does :func:`dispatch` open
    the one the handler runs in. Two committed transactions per ``long_running``
    dispatch is the cost, and is why minting is gated on the declaration rather than
    done for every call.

    **The one clock read in this module.** ``dispatch()`` takes no instant and its
    callers are HTTP listeners with none to give it, so wall time enters the system
    here, the way it already does in ``storage/work_index.py``. Everything downstream
    of this row — the worker's terminal write, the terminal check — takes its instant
    from a caller, so the discipline the repositories keep is unaffected.

    A refusal out of ``open_unit_of_work`` propagates to the caller, which folds it
    into the same refusal outcome an unroutable workspace already produces. Nothing is
    minted for a workspace the call could not have reached anyway.
    """
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
            now=datetime.now(UTC),
        )
        uow.commit()
    return operation_id


def _fail_operation(
    ctx: WorkspaceContext,
    operation_id: UUID | None,
    *,
    error_code: str,
    error_text: str,
) -> None:
    """End a minted record ``failed``, in a short transaction of its own.

    **Why the dispatcher writes a terminal state at all**, when the rule is that the
    worker is the terminaliser: the worker only ever sees a record whose job exists,
    and every exit that reaches here is one where no job does. :func:`_mint_operation`
    committed on its own, so the work transaction's rollback leaves the record behind;
    the caller has been told the dispatch failed; and nothing downstream will ever look
    at that row again. ``pending`` for ever is the one answer criterion 13 forbids,
    because the caller cannot tell it from work still in flight. **This never writes
    ``succeeded``** — that stays the worker's alone, and is why the record's success
    path still has exactly one writer.

    ``operation_id`` is ``None`` for every declaration that is not ``long_running``,
    which is every operation release one registers, so the common case returns without
    touching the database and the call sites need no ``if`` of their own.

    **Its own unit of work, and it may fail.** The work transaction has already been
    rolled back before any call site reaches here, so this opens a fresh one rather
    than reusing a dead connection. If the workspace has become unreachable between the
    mint and now, the record stays ``pending`` and the line is logged: ``dispatch()``
    answers its caller rather than raising, exactly as it does for a handler that
    failed, and a bounded loss on an unreachable database is the same trade
    ``events/publish.py``'s ``mark_due_after_publish`` documents one layer out.
    """
    if operation_id is None:
        return
    try:
        with open_unit_of_work(ctx) as uow:
            applied = finish_failed(
                uow.connection,
                operation_id=operation_id,
                now=datetime.now(UTC),
                error_code=error_code,
                error_text=error_text[:_DETAIL_LIMIT],
            )
            uow.commit()
    except Exception:
        logger.error(
            "operation_record_left_pending",
            extra={
                "operation_id": str(operation_id),
                "workspace_id": str(ctx.workspace_id),
                "request_id": str(ctx.request_id),
            },
        )
        return
    if not applied:
        # The write is predicated on the record still being open, so a zero rowcount
        # means something else terminalised it between the mint and here. Nothing in
        # release one can: no job carries this id on any path that reaches this
        # function. Logged rather than raised, because the record is terminal either
        # way and the outcome the caller is about to receive is unaffected.
        logger.warning(
            "operation record %s was already terminal when its dispatch failed; "
            "the record is left as it was",
            operation_id,
        )


def _output_invalid(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    operation_id: UUID | None,
    audit: _AuditWrite | None,
    *,
    detail: str,
) -> OperationOutcome:
    """Roll the work back, end any minted record, audit the failure, and answer
    ``output_invalid``.

    Two branches reach it — a handler whose return value is not the declared output
    type, and a ``long_running`` handler that queued no job — and they are one
    function rather than two copies because the ordering is the part that has to be
    right at both: roll back first, so the two writes below each open their own
    transaction against a connection this one is no longer holding.

    The audit row's outcome is ``failed`` rather than ``refused``, matching the
    outcome this function returns: the caller asked for something the system was
    willing to do and the operation did not deliver it, which is a failure, not a
    refusal to act.

    The outcome's ``state`` stays ``failed`` with ``output_invalid`` as the code,
    which is the shape this branch has answered since 0b1 and which the API envelope's
    status map already knows.
    """
    _rollback(uow)
    _fail_operation(ctx, operation_id, error_code=OUTPUT_INVALID, error_text=detail)
    _audit_alone(ctx, audit, outcome=AUDIT_FAILED, operation_id=operation_id)
    return OperationOutcome(
        FAILED,
        error=OperationError(OUTPUT_INVALID, detail),
        operation_id=operation_id,
    )


def _rollback(uow: UnitOfWork) -> None:
    # A commit that failed has already closed the transaction; the ``with`` exit
    # rolls back anything still open, so a closed unit of work is not an error here.
    try:
        uow.rollback()
    except StorageRefusal:
        pass


def dispatch(
    ctx: object,
    name: str,
    payload: Mapping[str, object] | None = None,
    *,
    registry: OperationRegistry = REGISTRY,
) -> OperationOutcome:
    """Run ``name`` for ``ctx`` with ``payload``; see the module docstring."""
    if not isinstance(ctx, WorkspaceContext):
        # Structurally outside the audit set, the first of three: there is no
        # ``WorkspaceContext`` yet, so there is no workspace to route a row to and
        # nothing that could open a connection to write one.
        return _refused(
            CONTEXT_REQUIRED,
            "dispatch() needs a WorkspaceContext from a boundary factory",
        )
    request_digest = _request_digest(payload)
    authorized = registry.authorize(ctx, name)
    if isinstance(authorized, Refusal):
        # ``operation_unknown`` is the second structural exclusion — ``lookup``
        # answers ``None`` for exactly that refusal, so ``_audit_write`` answers
        # ``None`` and no row is written. The other three refusals ``authorize``
        # returns resolved a declaration first and each gets its own row, written on
        # a fresh connection because no work transaction was ever opened.
        _audit_alone(
            ctx,
            _audit_write(registry.lookup(name), request_digest=request_digest),
            outcome=AUDIT_REFUSED,
        )
        return _refused(authorized.state, authorized.detail)
    operation = authorized.operation
    declaration = operation.declaration
    # Resolved **before** input validation and before the mint, so that every path
    # below this line is one a row can be written for. Refusing later would mean
    # owing an ``input_invalid`` row that nothing could write, and minting first
    # would leave a committed ``pending`` record for a call that is about to be
    # refused. ``None`` for a ``READ`` operation, which writes no row at all.
    audit = _audit_write(operation, request_digest=request_digest)
    if audit is None and declaration.safety_class is not SafetyClass.READ:
        return _refused(
            AUDIT_SINK_MISSING,
            f"no audit sink is installed for module {operation.module_id!r}, which "
            f"owns {name}; an operation above the read class does not run without "
            "one",
        )
    try:
        model_input = declaration.input_model.model_validate(
            {} if payload is None else payload
        )
    except ValidationError as exc:
        _audit_alone(ctx, audit, outcome=AUDIT_REFUSED)
        return _refused(INPUT_INVALID, _describe_validation_error(exc))
    # The subject can only be read off a *validated* model, so it joins the row here
    # rather than above. Every row written before this line therefore carries a null
    # ``subject_ref``, which is what the column is nullable for.
    if audit is not None:
        audit = replace(audit, subject_ref=_subject_ref(declaration, model_input))
    # Minted here and nowhere else: after authorization and validation have both
    # passed, and before the work transaction is opened, so the id is readable while
    # the work is still pending. ``None`` for every declaration that is not
    # ``long_running``.
    #
    # **The three refusals above this line — and only those three — leave no record
    # behind**, because they return before anything is written. Every exit below it
    # can, which is why each of them calls ``_fail_operation``; the claim is scoped to
    # the branches above rather than asserted over the function, so a later exit added
    # below cannot quietly falsify it.
    operation_id: UUID | None = None
    try:
        if declaration.long_running:
            operation_id = _mint_operation(ctx, declaration)
    except StorageRefusal as refusal:
        # Nothing was minted: this refusal came out of the mint's own unit of work,
        # which is ``open_unit_of_work`` refusing — the third structural exclusion,
        # reached one call site earlier. A row would need a connection to the same
        # workspace database that just refused this one, so there is none to write.
        return _refused(refusal.state, refusal.detail)
    try:
        uow = open_unit_of_work(ctx)
    except StorageRefusal as refusal:
        # The third structural exclusion at its own site: the unit of work is
        # unobtainable, and that is the refusal — a second attempt to open one just
        # for the audit row would meet the identical failure, so no row is written.
        #
        # The mint has already committed and the work cannot start, so the record is
        # orphaned unless it is ended here — and the caller is handed the id either
        # way, because a refusal that dropped it would leave a row nobody can name.
        # ``_fail_operation`` opens a unit of work of its own and may be refused for
        # the same reason this was; it logs and the record stays ``pending``.
        _fail_operation(
            ctx, operation_id, error_code=refusal.state, error_text=refusal.detail
        )
        return _refused(refusal.state, refusal.detail, operation_id=operation_id)
    # Deliberately: authorize, the audit-sink resolution, the mint for a long-running
    # declaration, one unit of work, the handler, the output-type check, the audit
    # row, commit. The audit row is the **last** thing written before the commit and
    # the only thing between the handler and it; no outbox event is enqueued here.
    #
    # There is no safety-class branch on the *running* of an operation and this
    # function adds none: ``read``, ``draft`` and ``mutate`` all simply run. The one
    # class branch is over the audit row, which every class but ``read`` gets. Run 0v's
    # finding F14 records that ``overview.md`` reads as though the class were enforced
    # today.
    with uow:
        try:
            # The handler gets the sealed view, carrying the minted id so a
            # long-running handler can stamp it on the job it enqueues; ending the
            # transaction is this function's job, on the real ``uow``.
            output = operation.handler(
                ctx,
                HandlerUnitOfWork(uow, operation_id=operation_id),
                model_input,
            )
            if not isinstance(output, declaration.output):
                return _output_invalid(
                    ctx,
                    uow,
                    operation_id,
                    audit,
                    detail=(
                        f"{name} returned {type(output).__name__}, not "
                        f"{declaration.output.__name__}"
                    ),
                )
            if operation_id is not None and not has_job_for_operation(
                uow.connection, operation_id=operation_id
            ):
                # A ``long_running`` declaration is a promise that the work is queued
                # and that the record will be terminalised by the worker that runs it.
                # A handler that returned a well-formed output without enqueuing
                # anything has kept the shape of that promise and none of its content:
                # no job carries the id, so no worker will ever visit the record. Read
                # inside the work transaction, before the commit, so the job the
                # handler *did* enqueue is visible and only its real absence refuses.
                return _output_invalid(
                    ctx,
                    uow,
                    operation_id,
                    audit,
                    detail=(
                        f"{name} is long-running and queued no job for its operation "
                        f"record, which nothing would then terminalise"
                    ),
                )
            # **Inside the operation's own transaction, and last.** The row commits
            # with the effects, so an operation whose own write fails leaves neither
            # its effect nor its audit record — the property that makes the record
            # worth trusting (AC 23). Last, so that a handler that has already
            # refused, raised or returned the wrong type never reaches it: those
            # outcomes are audited from the ``except`` blocks below, after the
            # rollback, exactly once each. A ``long_running`` dispatch is audited
            # ``succeeded`` here too — what succeeded is the dispatch, which queued
            # the work; whether the *work* succeeds is the operation record's
            # question and the worker's to answer.
            if audit is not None:
                audit.record(
                    ctx, uow, outcome=AUDIT_SUCCEEDED, operation_id=operation_id
                )
            uow.commit()
        except OperationRefused as refusal:
            _rollback(uow)
            _fail_operation(
                ctx,
                operation_id,
                error_code=refusal.state,
                error_text=refusal.detail or refusal.state,
            )
            _audit_alone(ctx, audit, outcome=AUDIT_REFUSED, operation_id=operation_id)
            return _refused(refusal.state, refusal.detail, operation_id=operation_id)
        except StorageRefusal as refusal:
            _rollback(uow)
            _fail_operation(
                ctx, operation_id, error_code=refusal.state, error_text=refusal.detail
            )
            _audit_alone(ctx, audit, outcome=AUDIT_REFUSED, operation_id=operation_id)
            return _refused(refusal.state, refusal.detail, operation_id=operation_id)
        except Exception as exc:
            _rollback(uow)
            # The exception text is for the log, never the outcome: a driver error
            # renders the statement and its parameters, and ``error_text`` is
            # documented as safe to show. ``logger.exception`` implies
            # ``exc_info=True``, which would carry that same formatted traceback
            # into the log record; ``logger.error`` with only the exception's class
            # name keeps the log line as safe as the outcome already is.
            logger.error(
                "operation_failed",
                extra={
                    "operation": name,
                    "workspace_id": str(ctx.workspace_id),
                    "request_id": str(ctx.request_id),
                    "exception_type": type(exc).__name__,
                },
            )
            _fail_operation(
                ctx,
                operation_id,
                error_code=HANDLER_FAILED,
                error_text=type(exc).__name__,
            )
            # ``failed`` rather than ``refused``: the handler did not decline, it
            # broke. This branch also catches a commit that raised, so the audit row
            # written here can be the row for a dispatch whose own transaction — and
            # therefore whose in-transaction ``succeeded`` row — never landed. Still
            # exactly one row, because that one went down with the rollback.
            _audit_alone(ctx, audit, outcome=AUDIT_FAILED, operation_id=operation_id)
            return OperationOutcome(
                FAILED,
                error=OperationError(HANDLER_FAILED, type(exc).__name__),
                operation_id=operation_id,
            )
    # ``pending`` for a ``long_running`` declaration, and this function writes no
    # other state for one: the work runs as a job and the worker that finishes that
    # job is the only thing that moves the record out of ``pending``. Returning
    # ``succeeded`` here would claim the work was done at the moment it was merely
    # scheduled.
    if operation_id is not None:
        return OperationOutcome(PENDING, result=output, operation_id=operation_id)
    return OperationOutcome(SUCCEEDED, result=output)
