"""``core.operation.get`` / ``.list`` / ``.resolve``: their models and handlers,
beside the repository they read.

The declarations and the registration live in ``operations/core_ops.py``; the models
and the handlers live **here**. That is the split ``work/operations.py`` already
established for ``core.work.failures``, and it is what keeps ``core_ops.py`` a list of
declarations rather than a second home for every operation's payload shapes.

**Every read of an operation record goes through these two operations.** AC 19's "no
test in this run reads ``core.operation`` by querying the table" is a property of the
tests, but it is only *available* to them because ``get`` and ``list`` exist and answer
the whole question: the record's state, its outcome, and its terminal check.

**``limit`` is bounded at the model, never at the query** — :class:`OperationListInput`
mirrors ``work/operations.py``'s ``FailureListInput`` for the same stated reason: a
bare ``int`` lets ``-1`` reach the ``SELECT``, where Postgres raises *LIMIT must not be
negative*, and ``dispatch`` folds that into ``handler_failed`` carrying an exception
class name, when it is an input defect that belongs in the ``input_invalid`` refusal
pydantic already produces.

**The two ``Literal`` aliases below are checked against the repository's own tuples at
import time.** A ``Literal`` is what puts a real enum in the generated OpenAPI document
and what makes an unknown value an ``input_invalid`` refusal rather than a row with a
misspelled state in it; the import-time check is what keeps the two spellings from
being two sources of truth. Widening ``records.RESOLVABLE_OUTCOMES`` or
``records.TERMINAL_CHECK_KINDS`` without widening the alias fails at import, loudly,
rather than at the first call that uses the new member.
"""

from datetime import UTC, datetime
from typing import Final, Literal, Self, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rheo_contracts import WorkspaceContext

from rheo_core.operations.records import (
    RESOLVABLE_OUTCOMES,
    SUCCEEDED,
    TERMINAL_CHECK_KINDS,
    UNRESOLVED,
    OperationRow,
)
from rheo_core.operations.records import (
    get as read_operation,
)
from rheo_core.operations.records import (
    list_recent as list_operations,
)
from rheo_core.operations.records import (
    resolve as resolve_operation,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import NOT_FOUND
from rheo_core.storage.backend import UnitOfWork

OPERATION_GET: Final = "core.operation.get"
OPERATION_LIST: Final = "core.operation.list"
OPERATION_RESOLVE: Final = "core.operation.resolve"
"""Written whole, deliberately.

Run 0c0 cut this operation and listed its name among the tokens its boundary guard
scanned for; ``tests/test_absent_behaviour.py`` assembled the same string from two
literals so that naming it in a probe would not fire that guard on 0c0's own diff.
Run 0c2 is the run that builds it, so the name belongs in the tree as a plain literal.
Splitting it here would hide nothing anyway — the generated OpenAPI document carries
the registered name verbatim — and would leave a later reader hunting for an operation
that ``grep`` cannot find."""

OPERATION_STATE: Final = "operation_state"
"""The refusal state for an operation record that is in the wrong state for the act
asked of it. Named after ``storage/backend.py``'s ``workspace_state``, which is the
same idea one layer out."""

ResolutionOutcome = Literal["succeeded", "failed", "cancelled"]
TerminalCheckKind = Literal[
    "record_exists", "provider_status", "sink_recorded", "handler_returned"
]

if set(get_args(ResolutionOutcome)) != set(  # pragma: no cover - import-time invariant
    RESOLVABLE_OUTCOMES
):
    raise RuntimeError(
        "ResolutionOutcome and records.RESOLVABLE_OUTCOMES disagree; they are the "
        "same set spelled twice and must be widened together"
    )

if set(get_args(TerminalCheckKind)) != set(  # pragma: no cover - import-time invariant
    TERMINAL_CHECK_KINDS
):
    raise RuntimeError(
        "TerminalCheckKind and records.TERMINAL_CHECK_KINDS disagree; they are the "
        "same set spelled twice and must be widened together"
    )

# See ``rheo_core.operations.core_ops`` for why ``ignore`` (the default) is stated.
_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")


class OperationRef(BaseModel):
    """Which record to read. ``operation_id`` is not a reserved input field, so a
    declaration may name it."""

    model_config = _IGNORE_EXTRA

    operation_id: UUID


class OperationListInput(BaseModel):
    """How many of the most recently created records to return."""

    model_config = _IGNORE_EXTRA

    limit: int = Field(default=50, ge=1, le=500)


class OperationResolveInput(BaseModel):
    """An ``unresolved`` record, the outcome a person has decided it had, and why.

    ``note`` is required and non-empty: AC 18's whole content is that clearing an
    ``unresolved`` record is an *explicit act*, and a resolution with no stated reason
    is indistinguishable from an accidental one. It is bounded so a note cannot become
    an unbounded write into a column nothing prunes.
    """

    model_config = _IGNORE_EXTRA

    operation_id: UUID
    outcome: ResolutionOutcome
    note: str = Field(min_length=1, max_length=2000)
    terminal_check_kind: TerminalCheckKind | None = None

    @model_validator(mode="after")
    def _succeeded_names_its_terminal_check(self) -> Self:
        """A resolution to ``succeeded`` must say what was checked.

        Refused here rather than left to the database's own
        ``operation_terminal_check_required`` constraint: the constraint would raise
        at the write, which ``dispatch`` folds into ``handler_failed`` carrying a
        driver class name, and the caller's actual mistake — a missing field — is an
        ``input_invalid`` refusal that names the field.
        """
        if self.outcome == SUCCEEDED and self.terminal_check_kind is None:
            raise ValueError(
                "resolving an operation to 'succeeded' requires terminal_check_kind: "
                "no operation may report succeeded without a recorded terminal check"
            )
        return self


class OperationRecord(BaseModel):
    """One operation record as these operations publish it.

    :class:`~rheo_core.operations.records.OperationRow` field for field, with that
    row's ``id`` published as ``operation_id`` — the same renaming
    ``work/operations.py``'s ``FailedJob`` makes for the same reason: the response is
    about one operation and the longer name reads unambiguously in a generated client.
    """

    model_config = ConfigDict(frozen=True)

    operation_id: UUID
    name: str
    safety_class: str
    state: str
    result_ref: str | None
    error_code: str | None
    error_text: str | None
    terminal_check_kind: str | None
    terminal_check_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    terminal_at: datetime | None


class OperationList(BaseModel):
    """The workspace's most recently created operation records, newest first.

    ``operations`` is a named collection field rather than a bare list, mirroring
    ``FailureList``: a later run adding a second collection beside it is then an
    additive change a reader may ignore, not a change to the response's own type.
    """

    model_config = ConfigDict(frozen=True)

    operations: list[OperationRecord]


def _published(row: OperationRow) -> OperationRecord:
    return OperationRecord(
        operation_id=row.id,
        name=row.name,
        safety_class=row.safety_class,
        state=row.state,
        result_ref=row.result_ref,
        error_code=row.error_code,
        error_text=row.error_text,
        terminal_check_kind=row.terminal_check_kind,
        terminal_check_at=row.terminal_check_at,
        created_at=row.created_at,
        started_at=row.started_at,
        terminal_at=row.terminal_at,
    )


def _must_read(uow: UnitOfWork, operation_id: UUID) -> OperationRow:
    """The record, or an ``operation_unknown``-shaped refusal naming it.

    ``not_found`` rather than an empty record, and rather than ``None`` returned to a
    caller that would then have to invent the distinction. The workspace is the
    context's own — ``uow.connection`` is already routed to it — so an id minted in
    another workspace is simply not here, which is the same answer a deleted one gets.
    """
    row = read_operation(uow.connection, operation_id=operation_id)
    if row is None:
        raise OperationRefused(
            NOT_FOUND, f"no operation {operation_id} in this workspace"
        )
    return row


def get_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: OperationRef
) -> OperationRecord:
    """One operation record of the context's own workspace."""
    return _published(_must_read(uow, model_input.operation_id))


def list_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: OperationListInput
) -> OperationList:
    """The most recently created operation records of the context's own workspace."""
    return OperationList(
        operations=[
            _published(row)
            for row in list_operations(uow.connection, limit=model_input.limit)
        ]
    )


def resolve_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: OperationResolveInput
) -> OperationRecord:
    """Clear an ``unresolved`` record to a named outcome with a note.

    **Read, then write, and the write carries its own predicate.** The read is what
    lets the refusal say *which* wrong state the record is in — "no such record" and
    "already succeeded" are different mistakes and a caller acts on them differently.
    The predicate on the write is what makes the refusal true anyway if the record
    moved between the two statements; without it, the read would be a check a
    concurrent resolution could walk straight through.

    ``datetime.now(UTC)`` here for the same reason ``dispatch()`` reads it to mint:
    this is a handler reached through the dispatcher, which takes no instant, and the
    resolution's instant is the moment a person acted.
    """
    row = _must_read(uow, model_input.operation_id)
    if row.state != UNRESOLVED:
        raise OperationRefused(
            OPERATION_STATE,
            f"operation {model_input.operation_id} is {row.state!r}; only an "
            "'unresolved' operation can be resolved",
        )
    applied = resolve_operation(
        uow.connection,
        operation_id=model_input.operation_id,
        outcome=model_input.outcome,
        note=model_input.note,
        now=datetime.now(UTC),
        terminal_check_kind=model_input.terminal_check_kind,
    )
    if not applied:
        raise OperationRefused(
            OPERATION_STATE,
            f"operation {model_input.operation_id} left 'unresolved' while it was "
            "being resolved; nothing was written",
        )
    return _published(_must_read(uow, model_input.operation_id))
