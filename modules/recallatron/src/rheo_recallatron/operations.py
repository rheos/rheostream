"""Recallatron's read operations: first-cut lexical ``recall`` and the centered
``read`` window.

Both are ``READ``, both are owner/member/service (§ A10), and neither carries an audit
spec because the read class needs none. Every permission decision either makes is
:func:`~rheo_recallatron.eligibility.eligible_memory`'s; what lives here is input
validation, the lexical query, the five-step read precedence and the shape of the
answer.

**The read precedence is the security core of this module, and its order is the
guarantee.** § A6:

1. Validate the references, the target's type, the context width, the read mode and
   the authoritative purpose binding, then the retention policy. Malformed input is
   ``input_invalid``; a well-formed purpose that is not this context's binding is
   ``purpose_mismatch``; a missing or corrupt retention policy is
   ``retention_unavailable``. All three land before anything looks at a container.
2. Authorize the **exact target** through full eligibility in the requested mode.
   Missing, cross-workspace, expired or ineligible is ``not_found``, whatever the
   container holds; a spent reference budget is ``reference_scan_limit``.
3. Prove exact stored membership of the named container, by existence check. Absent is
   ``container_membership_required``, before a single neighbour is selected.
4. Only then select member identifiers, prefiltered row-locally, ordered, ``LIMIT
   501``.
5. A 501st identifier returns the fixed content-free ``window_scan_limit`` and nothing
   else.

Read it downward and the property falls out: the one operational signal a saturated
container emits is unreachable through an invalid, ineligible or non-member target,
because each of those refusals is answered by an earlier step. An implementation that
reordered these would still pass a test suite that only checked each refusal in
isolation, which is why the paired small-container/over-cap-container cases exist.

**Refusals are raised, not returned.** The dispatcher turns
:class:`~rheo_core.operations.refusals.OperationRefused` into an outcome whose state is
the refusal code and rolls the transaction back, which is the established error path
(§ A10) — there is no second envelope here, and no success model carries a refusal
field.
"""

from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from rheo_contracts import (
    ContextPurpose,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    RecordRefMalformed,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import ColumnElement, func, literal_column, select

from rheo_recallatron.configuration import (
    CANDIDATE_SCAN_LIMIT,
    CANDIDATE_SENTINEL_LIMIT,
    MEMORY_RECORD_TYPE,
    MODULE_ID,
    READ_CONTEXT_DEFAULT,
    READ_CONTEXT_MAX,
    READ_CONTEXT_MIN,
    RECALL_K_DEFAULT,
    RECALL_K_MAX,
    RECALL_K_MIN,
    RECALL_QUERY_MAX_LENGTH,
    RECALL_QUERY_MIN_LENGTH,
)
from rheo_recallatron.eligibility import (
    READ_ROLES,
    Denied,
    Eligible,
    MemoryRequest,
    ReadMode,
    begin_request,
    container_candidates,
    eligible_memory,
    evaluate_all,
    is_container_member,
    memory_reference,
    row_local_conditions,
)
from rheo_recallatron.refusals import (
    CONTAINER_MEMBERSHIP_REQUIRED,
    INPUT_INVALID,
    NOT_FOUND,
    PURPOSE_MISMATCH,
    REFERENCE_SCAN_LIMIT,
    RETENTION_UNAVAILABLE,
    WINDOW_SCAN_LIMIT,
)
from rheo_recallatron.storage import tables as t

MEMORY_RECALL: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}.recall"
MEMORY_READ: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}.read"

STRATEGY_LEXICAL: Final = "lexical"
"""What every item this run returns is marked with.

Not the configurable retrieval adapter, and not a claim on the ranked-retrieval
criterion: first-cut recall is lexical search over the eligible candidate set, and
saying so on every row is what stops a later reader mistaking it for tuned retrieval.
"""

_SEARCH_CONFIG: Final[ColumnElement[Any]] = literal_column("'english'::regconfig")
"""The text-search configuration the query must use.

It has to be the one the stored generated column was built with — see the
``search_tsv`` column in this package's tables module — or the query would be matched
against lexemes produced by a different dictionary. Cast explicitly for the same
reason the generated column casts: the one-argument form reads a session setting and
is only ``STABLE``.
"""


# --- input and output models ----------------------------------------------------------


class _Strict(BaseModel):
    """Extra fields forbidden, frozen. Every model below is one.

    ``extra = "forbid"`` is § A13's rule for this module's typed contracts and is also
    what makes an unknown key ``input_invalid`` at the dispatcher rather than a
    silently ignored argument.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryLinkHead(_Strict):
    """One of a memory's links as this caller may see it.

    ``display`` is ``None`` for a marked lineage link: ancestry is content-free, so the
    reference travels and the predecessor's head never does.
    """

    ref: str
    relation: str
    supersession_lineage: bool
    display: str | None


class MemoryItem(_Strict):
    """One memory, with the lifecycle fields § A6 says a read returns.

    ``superseded_by`` is present only when the successor is itself eligible to this
    caller right now — § A6's "successor ref only when safely showable". A replacement
    the caller may not read is not named.
    """

    ref: str
    kind: str
    title: str
    body: str
    occurred_at: datetime | None
    recorded_at: datetime
    revision: int
    invalidated_at: datetime | None
    invalidation_reason: str | None
    superseded_by: str | None
    links: tuple[MemoryLinkHead, ...]


class RecallItem(MemoryItem):
    """A recall hit: a memory plus the score and strategy that found it."""

    score: float
    strategy: str


class RecallInput(_Strict):
    query: str = Field(
        min_length=RECALL_QUERY_MIN_LENGTH, max_length=RECALL_QUERY_MAX_LENGTH
    )
    k: int = Field(default=RECALL_K_DEFAULT, ge=RECALL_K_MIN, le=RECALL_K_MAX)
    include_invalidated: bool = False
    purpose: str | None = None

    @field_validator("query")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        """§ A13's "nonblank" half. There is no no-query recency bundle: a query of
        spaces is not a request for everything, it is a malformed request."""
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class RecallResult(_Strict):
    items: tuple[RecallItem, ...]


class ReadInput(_Strict):
    container_ref: str
    target_ref: str
    context: int = Field(
        default=READ_CONTEXT_DEFAULT, ge=READ_CONTEXT_MIN, le=READ_CONTEXT_MAX
    )
    include_invalidated: bool = False
    purpose: str | None = None


class ReadWindow(_Strict):
    """A successful centered window, and only ever a successful one.

    Every field here is computed from the **fully eligible** ordering, so none of them
    counts a member this caller may not see. There is no field for a refusal: a refused
    read raises, and the caller is told a state rather than handed a half-window.
    """

    items: tuple[MemoryItem, ...]
    target_position: int
    window_start: int
    window_end: int
    total: int
    has_more: bool


# --- step 1: input, purpose binding and retention ------------------------------------


def _canonical(reference: str) -> RecordRef:
    try:
        return RecordRef.parse(reference)
    except (RecordRefMalformed, TypeError):
        raise OperationRefused(INPUT_INVALID, "a reference must be canonical") from None


def _memory_target(reference: str) -> RecordRef:
    parsed = _canonical(reference)
    if parsed.module != MODULE_ID or parsed.record_type != MEMORY_RECORD_TYPE:
        raise OperationRefused(INPUT_INVALID, "the target must be a memory reference")
    return parsed


def _checked_purpose(ctx: WorkspaceContext, stated: str | None) -> None:
    """The authoritative binding is the context's, and a stated purpose is checked
    against it rather than replacing it.

    Omission resolves to the binding, which is what a bound caller normally does. A
    value outside the closed vocabulary is ``input_invalid``; a well-formed one that
    disagrees with the binding is ``purpose_mismatch`` — including the case where the
    context carries no binding at all, because a caller cannot narrow to a purpose this
    boundary never verified it holds. Neither refusal echoes the value.
    """
    if stated is None:
        return
    try:
        wanted = ContextPurpose(stated)
    except ValueError:
        raise OperationRefused(
            INPUT_INVALID, "purpose is not one of the declared context purposes"
        ) from None
    if wanted is not ctx.principal.bound_purpose:
        raise OperationRefused(
            PURPOSE_MISMATCH, "the stated purpose is not this context's binding"
        )


def _opened(ctx: WorkspaceContext, uow: UnitOfWork) -> MemoryRequest:
    request = begin_request(ctx, uow)
    if request is None:
        raise OperationRefused(
            RETENTION_UNAVAILABLE,
            "this workspace has no usable retention policy",
        )
    return request


def _mode(include_invalidated: bool) -> ReadMode:
    return ReadMode.HISTORY if include_invalidated else ReadMode.CURRENT


def _refuse(denied: Denied) -> OperationRefused:
    """A denial as the caller hears it. No detail beyond the state's own meaning: a
    refusal that explained *which* check failed would be the metadata the refusal
    exists to withhold."""
    if denied.state == REFERENCE_SCAN_LIMIT:
        return OperationRefused(
            REFERENCE_SCAN_LIMIT, "this read exceeded its reference budget"
        )
    return OperationRefused(NOT_FOUND, "no such memory")


# --- rendering -----------------------------------------------------------------------


def _successor(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    row_successor: UUID | None,
    *,
    request: MemoryRequest,
) -> str | None:
    """The successor reference, when the successor is itself readable now.

    Evaluated in ``current`` mode and charged to the same shared budget: a replacement
    is a live row or it is not named. A budget exhausted here refuses the whole read
    rather than returning a window with one field quietly dropped.
    """
    if row_successor is None:
        return None
    decision = eligible_memory(
        ctx, uow, row_successor, mode=ReadMode.CURRENT, request=request
    )
    if isinstance(decision, Denied):
        if decision.state == REFERENCE_SCAN_LIMIT:
            raise _refuse(decision)
        return None
    return memory_reference(row_successor)


def _item(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    eligible: Eligible,
    *,
    request: MemoryRequest,
) -> MemoryItem:
    row = eligible.row
    return MemoryItem(
        ref=memory_reference(row.id),
        kind=row.kind,
        title=row.title,
        body=row.body,
        occurred_at=row.occurred_at,
        recorded_at=row.recorded_at,
        revision=row.revision,
        invalidated_at=row.invalidated_at,
        invalidation_reason=row.invalidation_reason,
        superseded_by=_successor(ctx, uow, row.superseded_by_id, request=request),
        links=tuple(
            MemoryLinkHead(
                ref=link.ref,
                relation=link.relation,
                supersession_lineage=link.supersession_lineage,
                display=link.display,
            )
            for link in eligible.links
        ),
    )


# --- the handlers --------------------------------------------------------------------


def recall(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: RecallInput
) -> RecallResult:
    """Non-empty-query lexical search over the eligible candidate set.

    The SQL narrows row-locally and scores; eligibility decides. Candidates are walked
    in score order and each is fully evaluated — links included — before any of its
    content is put in the answer, so a row whose source the caller may not read never
    contributes a title. The walk stops at ``k`` eligible rows or at the bounded
    candidate scan, whichever comes first.

    No no-query recency bundle, no implicit session expansion, no dense fallback: the
    strategy is ``lexical`` and says so on every row.
    """
    _checked_purpose(ctx, model_input.purpose)
    request = _opened(ctx, uow)
    mode = _mode(model_input.include_invalidated)

    query = func.plainto_tsquery(_SEARCH_CONFIG, model_input.query)
    score = func.ts_rank_cd(t.memory.c.search_tsv, query)
    statement = (
        select(t.memory.c.id, score.label("score"))
        .where(
            t.memory.c.search_tsv.bool_op("@@")(query),
            *row_local_conditions(request, mode),
        )
        .order_by(score.desc(), t.memory.c.recorded_at, t.memory.c.id)
        .limit(CANDIDATE_SCAN_LIMIT)
    )

    items: list[RecallItem] = []
    for candidate, candidate_score in uow.connection.execute(statement).all():
        if len(items) == model_input.k:
            break
        decision = eligible_memory(ctx, uow, candidate, mode=mode, request=request)
        if isinstance(decision, Denied):
            if decision.state == REFERENCE_SCAN_LIMIT:
                raise _refuse(decision)
            continue
        items.append(
            RecallItem(
                # The same rendering every read uses, plus the two fields only a
                # search answer carries. Re-validated from its own dump rather than
                # built twice, so a field added to ``MemoryItem`` cannot land on a
                # window and be forgotten on a hit.
                **_item(ctx, uow, decision, request=request).model_dump(),
                score=float(candidate_score),
                strategy=STRATEGY_LEXICAL,
            )
        )
    return RecallResult(items=tuple(items))


def read(ctx: WorkspaceContext, uow: UnitOfWork, model_input: ReadInput) -> ReadWindow:
    """The bounded centered window around one authorized, proven-member target.

    The five numbered steps of this module's docstring, in that order and with nothing
    between them.
    """
    # 1. Input, target type, purpose binding, retention. Before any container is named.
    container = _canonical(model_input.container_ref).format()
    target = _memory_target(model_input.target_ref)
    _checked_purpose(ctx, model_input.purpose)
    request = _opened(ctx, uow)
    mode = _mode(model_input.include_invalidated)

    # 2. The exact target, fully authorized, whatever the container holds.
    authorized = eligible_memory(ctx, uow, target.id, mode=mode, request=request)
    if isinstance(authorized, Denied):
        raise _refuse(authorized)

    # 3. Exact stored membership, as an existence check, before any neighbour.
    if not is_container_member(uow, target.id, container):
        raise OperationRefused(
            CONTAINER_MEMBERSHIP_REQUIRED,
            "that memory is not linked to that container",
        )

    # 4. Ordered member identifiers, prefiltered row-locally, with the sentinel.
    candidates = container_candidates(uow, request, container_ref=container, mode=mode)

    # 5. The sentinel first, then full evaluation of at most 500.
    if len(candidates) == CANDIDATE_SENTINEL_LIMIT:
        raise OperationRefused(
            WINDOW_SCAN_LIMIT, "this container holds more members than a read scans"
        )
    eligible, overflowed = evaluate_all(
        ctx, uow, candidates, mode=mode, request=request
    )
    if overflowed is not None:
        raise _refuse(overflowed)

    order = [candidate for candidate, _ in eligible]
    if target.id not in order:
        # The target was eligible on its own and is not in the container's eligible
        # ordering: it is not a member of this window. No partials.
        raise OperationRefused(NOT_FOUND, "no such memory")
    total = len(order)
    position = order.index(target.id)
    window_start = max(0, position - model_input.context)
    window_end = min(total, position + model_input.context + 1)
    return ReadWindow(
        items=tuple(
            _item(ctx, uow, decision, request=request)
            for _, decision in eligible[window_start:window_end]
        ),
        target_position=position - window_start,
        window_start=window_start,
        window_end=window_end,
        total=total,
        has_more=window_start > 0 or window_end < total,
    )


# --- declarations -------------------------------------------------------------------


RECALL_DECLARATION: Final = OperationDeclaration(
    name=MEMORY_RECALL,
    safety_class=SafetyClass.READ,
    roles=READ_ROLES,
    input_model=RecallInput,
    output=RecallResult,
    idempotency=Idempotency.NONE,
    # ``READ``: an audit spec is required only above the read class, and a read that
    # wrote an audit row would put the caller's query text in a durable table, which
    # § A11 forbids even of the bounded telemetry sink.
    audit=None,
)

READ_DECLARATION: Final = OperationDeclaration(
    name=MEMORY_READ,
    safety_class=SafetyClass.READ,
    roles=READ_ROLES,
    input_model=ReadInput,
    output=ReadWindow,
    idempotency=Idempotency.NONE,
    audit=None,
)

OPERATIONS: Final[tuple[tuple[OperationDeclaration, Handler], ...]] = (
    (RECALL_DECLARATION, recall),
    (READ_DECLARATION, read),
)
"""What the manifest declares. The write operations join this tuple when they exist;
the record resolver is declared beside it on the manifest and shares the same
eligibility function."""
