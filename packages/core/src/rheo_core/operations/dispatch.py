"""``dispatch(ctx, name, payload) -> OperationOutcome``: authorize, validate, run in
one unit of work, commit.

The ``context_required`` check comes first, before any lookup: ``None`` or an object
that is not a ``WorkspaceContext`` is refused without the registry being consulted.
Then ``authorize``; then the payload is validated into the declaration's input model
(``input_invalid``, naming the failing fields but never echoing values); then **one**
``UnitOfWork`` is opened through ``route(ctx)``, the handler runs, and the unit of
work commits. A handler that raises :class:`OperationRefused` (or a
``StorageRefusal``) rolls back and yields that state; any other exception rolls back,
is logged with the context's ``request_id``, and yields ``failed`` with the fixed
code ``handler_failed`` and only the exception's class name as the text — never its
message, which for a driver error carries the statement and its parameters.

The handler receives a ``HandlerUnitOfWork`` — the same connection, with ``commit``,
``rollback`` and ``__enter__`` refused ``handler_may_not_commit``. Ending the one
transaction is this function's job.

An operation whose declaration carries an ``AuditSpec`` reaches one call to the audit
sink **registered for the operation's owning module**, inside the same transaction and
immediately before the commit. ``core.*`` resolves to ``NullAuditSink``, so core
behaves exactly as it did before the seam existed.

And nothing else. No audit *table*, no operation record, no outbox event, no
``operation_id`` minted — run 0c's scored work, deliberately absent here. See the
comment in the dispatcher body.

**The sink call itself is provisional, pending E0c.** The build plan's benchmark
schedule keeps audit-dispatch behaviour out of the 0c substrate, while run 0v's card
commissioned exactly this call; finding F55 records the conflict and its two
routes. This hook may be deleted at 0c0's branch cut rather than inherited, so do
not build on it without reading F55 first.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ValidationError
from rheo_contracts import AuditSpec, RecordRef, WorkspaceContext

from rheo_core.audit import sink_for
from rheo_core.boundary.context import CONTEXT_REQUIRED, Refusal
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
    """``state`` is ``succeeded`` with ``result``, or a refusal state / ``failed``
    with ``error``. There is no ``operation_id``: nothing here mints one, which is
    why the API envelope (C8) carries ``operation_id: null`` until 0c."""

    state: str
    result: BaseModel | None = None
    error: OperationError | None = None

    @property
    def ok(self) -> bool:
        return self.state == SUCCEEDED


def _refused(state: str, detail: str | None) -> OperationOutcome:
    return OperationOutcome(state, error=OperationError(state, detail or state))


def _describe_validation_error(exc: ValidationError) -> str:
    """The failing locations and messages, never the input values."""
    parts: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)[:_DETAIL_LIMIT]


def _subject_ref(model_input: BaseModel, audit: AuditSpec) -> RecordRef | None:
    """The subject reference ``audit`` names, read off the validated input.

    ``subject_field is None`` -> ``None``, which is every operation that declares an
    ``AuditSpec`` in release one: all four core mutate operations, and every *create*,
    whose subject does not exist until the handler has run. That ``AuditSpec`` cannot
    describe a create is run 0v's finding F4, carried to the note rather than patched
    here.

    Otherwise the named field is read and parsed into a ``RecordRef``, which is what
    the sink's signature takes. A field carrying something unparseable raises
    ``RecordRefMalformed`` into the dispatcher's own ``except Exception`` — rolled
    back and reported ``failed`` — rather than being silently dropped: a declaration
    naming a field that does not hold a reference is a wiring mistake, and swallowing
    it would write an audit row that quietly names nothing.
    """
    field = audit.subject_field
    if field is None:
        return None
    value = getattr(model_input, field, None)
    if value is None or isinstance(value, RecordRef):
        return value
    return RecordRef.parse(str(value))


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
    try:
        uow = open_unit_of_work(ctx)
    except StorageRefusal as refusal:
        return _refused(refusal.state, refusal.detail)
    # Deliberately: authorize, one unit of work, the handler, the owning module's
    # audit sink, commit. No operation record is minted, no outbox event is enqueued
    # and no job is scheduled. Those are run 0c's scored deliverables (the hard 0c
    # boundary). The sink writes **no core table** either: ``core.audit_record`` is
    # 0c's, ``sink_for`` returns ``NullAuditSink`` for every module without one, and
    # ``core`` never has one installed — so every core dispatch does exactly what it
    # did before this seam existed.
    #
    # There is no safety-class branch here and this function adds none: ``read``,
    # ``draft`` and ``mutate`` all simply run, and the only class-adjacent behaviour
    # is that a declaration carrying an ``AuditSpec`` reaches the sink. Classes above
    # ``mutate`` are 0c's. Run 0v's finding F14 records that ``overview.md`` reads as
    # though the class were enforced today.
    #
    # The sink call below is PROVISIONAL pending E0c: the build plan's benchmark
    # schedule keeps audit-dispatch behaviour out of the 0c substrate, and run 0v's
    # card commissioned it. Finding F55 carries the conflict and how to close it.
    with uow:
        try:
            # The handler gets the sealed view; everything below uses the real ``uow``.
            output = operation.handler(ctx, HandlerUnitOfWork(uow), model_input)
            if not isinstance(output, declaration.output):
                _rollback(uow)
                return OperationOutcome(
                    FAILED,
                    error=OperationError(
                        OUTPUT_INVALID,
                        f"{name} returned {type(output).__name__}, not "
                        f"{declaration.output.__name__}",
                    ),
                )
            audit = declaration.audit
            if audit is not None:
                # Two conditions, not one: the declaration carries an ``AuditSpec``,
                # **and** the sink is the one installed for the operation's owning
                # module. Keying on ``operation.module_id`` is what keeps a module's
                # sink off every other module's dispatches and off core's — see
                # ``rheo_core.audit.sink``. The real ``uow``, not the sealed view:
                # the sink is core, and its write belongs to this transaction.
                sink_for(operation.module_id).record(
                    ctx,
                    uow,
                    operation=name,
                    subject_ref=_subject_ref(model_input, audit),
                )
            uow.commit()
        except OperationRefused as refusal:
            _rollback(uow)
            return _refused(refusal.state, refusal.detail)
        except StorageRefusal as refusal:
            _rollback(uow)
            return _refused(refusal.state, refusal.detail)
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
                FAILED, error=OperationError(HANDLER_FAILED, type(exc).__name__)
            )
    return OperationOutcome(SUCCEEDED, result=output)
