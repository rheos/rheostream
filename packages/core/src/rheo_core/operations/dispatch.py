"""``dispatch(ctx, name, payload) -> OperationOutcome``: authorize, validate, run in
one unit of work, commit.

The ``context_required`` check comes first, before any lookup: ``None`` or an object
that is not a ``WorkspaceContext`` is refused without the registry being consulted.
Then ``authorize``; then the payload is validated into the declaration's input model
(``input_invalid``, naming the failing fields but never echoing values); then, for a
``long_running`` declaration and only for one, the operation record is minted and
committed on its own; then **one** work ``UnitOfWork`` is opened through
``route(ctx)``, the handler runs, and the unit of work commits. A handler that raises
:class:`OperationRefused` (or a
``StorageRefusal``) rolls back and yields that state; any other exception rolls back,
is logged with the context's ``request_id``, and yields ``failed`` with the fixed
code ``handler_failed`` and only the exception's class name as the text — never its
message, which for a driver error carries the statement and its parameters.

The handler receives a ``HandlerUnitOfWork`` — the same connection, with ``commit``,
``rollback`` and ``__enter__`` refused ``handler_may_not_commit``. Ending the one
transaction is this function's job.

**This function does not touch the audit seam at all.** Nothing runs between the
handler returning a valid output and the commit. ``rheo_core.audit`` still exports
the ``AuditSink`` protocol and the module-keyed registry a manifest fills at load
time, but nothing here reads that table: issue #45 keeps the contract as substrate
and leaves the call site — and what a missing registration means — to 0c2.

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

No audit row and no audit *table*, and no outbox event enqueued here — the audit half
is still deliberately absent. See the comment in the dispatcher body.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ValidationError
from rheo_contracts import OperationDeclaration, WorkspaceContext

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
from rheo_core.operations.registry import REGISTRY, OperationRegistry
from rheo_core.storage.backend import HandlerUnitOfWork, StorageRefusal, UnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.work.jobs import has_job_for_operation

logger = logging.getLogger("rheo_core.operations")

_DETAIL_LIMIT: Final = 2000


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
    *,
    detail: str,
) -> OperationOutcome:
    """Roll the work back, end any minted record, and answer ``output_invalid``.

    Two branches reach it — a handler whose return value is not the declared output
    type, and a ``long_running`` handler that queued no job — and they are one
    function rather than two copies because the ordering is the part that has to be
    right at both: roll back first, so the terminal write below opens its own
    transaction against a connection this one is no longer holding.

    The outcome's ``state`` stays ``failed`` with ``output_invalid`` as the code,
    which is the shape this branch has answered since 0b1 and which the API envelope's
    status map already knows.
    """
    _rollback(uow)
    _fail_operation(ctx, operation_id, error_code=OUTPUT_INVALID, error_text=detail)
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
        return _refused(
            CONTEXT_REQUIRED,
            "dispatch() needs a WorkspaceContext from a boundary factory",
        )
    authorized = registry.authorize(ctx, name)
    if isinstance(authorized, Refusal):
        return _refused(authorized.state, authorized.detail)
    operation = authorized.operation
    declaration = operation.declaration
    try:
        model_input = declaration.input_model.model_validate(
            {} if payload is None else payload
        )
    except ValidationError as exc:
        return _refused(INPUT_INVALID, _describe_validation_error(exc))
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
        # Nothing was minted: this refusal came out of the mint's own unit of work.
        return _refused(refusal.state, refusal.detail)
    try:
        uow = open_unit_of_work(ctx)
    except StorageRefusal as refusal:
        # The mint has already committed and the work cannot start, so the record is
        # orphaned unless it is ended here — and the caller is handed the id either
        # way, because a refusal that dropped it would leave a row nobody can name.
        # ``_fail_operation`` opens a unit of work of its own and may be refused for
        # the same reason this was; it logs and the record stays ``pending``.
        _fail_operation(
            ctx, operation_id, error_code=refusal.state, error_text=refusal.detail
        )
        return _refused(refusal.state, refusal.detail, operation_id=operation_id)
    # Deliberately: authorize, the mint for a long-running declaration, one unit of
    # work, the handler, the output-type check, commit — and nothing between the
    # handler and the commit. No audit row is written and no outbox event is
    # enqueued; those are still 0c's scored deliverables.
    #
    # There is no safety-class branch here and this function adds none: ``read``,
    # ``draft`` and ``mutate`` all simply run. Classes above ``mutate`` are 0c's. Run
    # 0v's finding F14 records that ``overview.md`` reads as though the class were
    # enforced today.
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
                    detail=(
                        f"{name} is long-running and queued no job for its operation "
                        f"record, which nothing would then terminalise"
                    ),
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
            return _refused(refusal.state, refusal.detail, operation_id=operation_id)
        except StorageRefusal as refusal:
            _rollback(uow)
            _fail_operation(
                ctx, operation_id, error_code=refusal.state, error_text=refusal.detail
            )
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
