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
returns ``state = "pending"`` without ever writing a terminal state for it. The worker
that finishes the job carrying the id is the only terminaliser. For every declaration
that is not ``long_running`` nothing is minted and ``operation_id`` stays ``None``,
exactly as before.

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
from rheo_core.operations.records import AUDIENCE_NONE, PENDING, mint
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
    # passed, so a refused call never leaves a record behind, and before the work
    # transaction is opened, so the id is readable while the work is still pending.
    # ``None`` for every declaration that is not ``long_running``.
    operation_id: UUID | None = None
    try:
        if declaration.long_running:
            operation_id = _mint_operation(ctx, declaration)
        uow = open_unit_of_work(ctx)
    except StorageRefusal as refusal:
        return _refused(refusal.state, refusal.detail)
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
                _rollback(uow)
                return OperationOutcome(
                    FAILED,
                    error=OperationError(
                        OUTPUT_INVALID,
                        f"{name} returned {type(output).__name__}, not "
                        f"{declaration.output.__name__}",
                    ),
                    operation_id=operation_id,
                )
            uow.commit()
        except OperationRefused as refusal:
            _rollback(uow)
            return _refused(refusal.state, refusal.detail, operation_id=operation_id)
        except StorageRefusal as refusal:
            _rollback(uow)
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
