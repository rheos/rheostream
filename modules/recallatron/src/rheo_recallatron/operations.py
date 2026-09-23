"""Recallatron's declared operations: ``recall`` through the retrieval seam, the
centered ``read`` window, the two writes — ``remember`` and ``derive`` — and the two
lifecycle changes, ``correct`` and ``supersede``.

The reads are ``READ`` and carry no audit spec, because the read class needs none. The
four writers are ``MUTATE`` and publish inside the same transaction as their row
writes and the dispatcher's success audit row. ``remember``/``derive`` carry
``AuditSpec(subject_field=None)`` and are owner/member/service;
``correct``/``supersede`` carry ``AuditSpec(subject_field="ref")`` — they act on a
record that already exists — and are owner/member alone (§ A10). Every permission
decision any of them makes is
:func:`~rheo_recallatron.eligibility.eligible_memory`'s; what lives here is input
validation, the dispatch to the configured retrieval strategy, the five-step read
precedence, the write ordering, and the shape of the answer. The write *machinery* —
audience ceiling, purpose rule, reference resolution, the row insert — is
``writes.py``'s, because the trusted source seam has to write a memory exactly the way
these two do, and the lifecycle closure itself is ``lifecycle.py``'s, because erasure
reaches it through the core's deletion coordinator rather than through an operation of
this module's.

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
from typing import Final
from uuid import UUID

from pydantic import Field, field_validator
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.deletion import lock_workspace_lifecycle
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.refs.resolver import UnitOfWork

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
from rheo_recallatron.contracts import (
    ArmCounts,
    CorrectInput,
    DeriveInput,
    MemoryCorrected,
    MemorySuperseded,
    MemoryWritten,
    RecallProvenance,
    RememberInput,
    Strict,
    SupersedeInput,
)
from rheo_recallatron.dedup import DEDUP_DECLARATION, dedup_candidates
from rheo_recallatron.eligibility import (
    LIFECYCLE_ROLES,
    READ_ROLES,
    Denied,
    Eligible,
    MemoryRequest,
    ReadMode,
    container_candidates,
    eligible_memory,
    evaluate_all,
    is_container_member,
    memory_reference,
)
from rheo_recallatron.embedding.operations import EMBEDDING_OPERATIONS
from rheo_recallatron.entities import ENTITY_OPERATIONS, resolve_mentions
from rheo_recallatron.events import consumers_for_dispatch
from rheo_recallatron.lifecycle import correct, supersede
from rheo_recallatron.references import canonical_ref, memory_target
from rheo_recallatron.refusals import (
    CONTAINER_MEMBERSHIP_REQUIRED,
    NOT_FOUND,
    REFERENCE_SCAN_LIMIT,
    WINDOW_SCAN_LIMIT,
)
from rheo_recallatron.retrieval.dispatch import resolve_strategy
from rheo_recallatron.retrieval.protocol import SearchRequest
from rheo_recallatron.writes import (
    ORIGIN_DERIVED,
    ORIGIN_TOLD,
    checked_purpose,
    opened,
    resolve_audience,
    resolve_purposes,
    resolve_sources,
    resolve_stated_links,
    write_memory,
)

MEMORY_RECALL: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}.recall"
MEMORY_READ: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}.read"
MEMORY_REMEMBER: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}.remember"
MEMORY_DERIVE: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}.derive"
MEMORY_CORRECT: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}.correct"
MEMORY_SUPERSEDE: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}.supersede"

WRITE_ROLES: Final = frozenset({Role.OWNER, Role.MEMBER, Role.SERVICE})
"""§ A10's roles for ``remember`` and ``derive``.

The same three members as :data:`~rheo_recallatron.eligibility.READ_ROLES` today, and
declared separately anyway: § A10's table gives ``correct``/``supersede`` only
owner/member — :data:`~rheo_recallatron.eligibility.LIFECYCLE_ROLES`, which they and
the memory's own delete share — so the write roles are not one set that happens to
equal the read set.
"""


# --- input and output models ----------------------------------------------------------


class MemoryLinkHead(Strict):
    """One of a memory's links as this caller may see it.

    ``display`` is ``None`` for a marked lineage link: ancestry is content-free, so the
    reference travels and the predecessor's head never does.
    """

    ref: str
    relation: str
    supersession_lineage: bool
    display: str | None


class MemoryItem(Strict):
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
    """A recall hit: a memory plus the score and strategy that found it.

    **``score`` is a different quantity under each strategy**, comparable only among
    the items of one response, and ``provenance.strategy`` says which one applies:

    - ``lexical``: ``ts_rank_cd`` cover density over the memory's text. Unbounded above,
      and its scale depends on the document's length and how densely it matches.
    - ``dense``: cosine similarity, ``1 - (vector <=> query)``, at or above the
      workspace's dense floor, because anything below it is withheld.
    - ``hybrid``: the reciprocal-rank-fusion sum, ``Σ 1 / (60 + rank)`` over the arms
      that found the memory. At most ``2/61`` with two arms, and on neither arm's scale.

    Since ``hybrid`` is the default, the default path's score is the fused sum, two
    orders of magnitude below a typical ``ts_rank_cd``. A score compared across
    strategies, or stored and compared later, compares nothing.

    ``strategy`` is the strategy that answered, and always equals
    ``provenance.strategy``.
    """

    score: float
    strategy: str


class RecallInput(Strict):
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


class RecallResult(Strict):
    items: tuple[RecallItem, ...]
    provenance: RecallProvenance


class ReadInput(Strict):
    container_ref: str
    target_ref: str
    context: int = Field(
        default=READ_CONTEXT_DEFAULT, ge=READ_CONTEXT_MIN, le=READ_CONTEXT_MAX
    )
    include_invalidated: bool = False
    purpose: str | None = None


class ReadWindow(Strict):
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


_canonical = canonical_ref
_memory_target = memory_target
"""Reference parsing moved to ``references.py`` when the write half arrived.

Aliased here rather than renamed at every call site below: three files now parse the
same references, and two of them would otherwise each answer ``input_invalid`` in
their own words. The names stay because the read precedence above cites them.
"""


_checked_purpose = checked_purpose
"""The authoritative binding is the context's, and a stated purpose is checked against
it rather than replacing it. Moved to ``writes.py`` beside :func:`opened` when
``dedup_candidates`` needed it too: that module is registered below, so it cannot
import this one."""


_opened = opened
"""The single retention read, shared with the write paths.

One function for reads and writes alike, so AC 8's "missing or corrupt retention
configuration refuses reads, writes, and sweep" cannot drift into two behaviours that
happen to agree today.
"""


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
    """Non-empty-query search over the eligible candidate set, ranked by this
    workspace's configured retrieval strategy.

    The strategy narrows row-locally and ranks; eligibility decides. Candidates are
    walked in rank order and each is fully evaluated — links included — before any of
    its content is put in the answer, so a row whose source the caller may not read
    never contributes a title. The walk stops at ``k`` eligible rows or at the bounded
    candidate scan, whichever comes first.

    No no-query recency bundle and no implicit session expansion. Every item and the
    response's ``provenance`` name the strategy that answered.
    """
    _checked_purpose(ctx, model_input.purpose)
    request = _opened(ctx, uow)
    mode = _mode(model_input.include_invalidated)

    strategy = resolve_strategy(ctx, uow)
    ranked = strategy.search(
        ctx,
        uow,
        SearchRequest(
            query=model_input.query,
            mode=mode,
            memory=request,
            limit=CANDIDATE_SCAN_LIMIT,
            k=model_input.k,
        ),
    )

    items: list[RecallItem] = []
    for hit in ranked.hits:
        if len(items) == model_input.k:
            break
        decision = eligible_memory(ctx, uow, hit.ref, mode=mode, request=request)
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
                score=hit.score,
                strategy=strategy.name,
            )
        )
    return RecallResult(
        items=tuple(items),
        provenance=RecallProvenance(
            strategy=strategy.name,
            arms=ArmCounts(lexical=ranked.arms.lexical, dense=ranked.arms.dense),
            dense_available=ranked.dense_available,
        ),
    )


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


# --- the write handlers ---------------------------------------------------------------
#
# Both are ``MUTATE``, both are owner/member/service, and both take the same shape: take
# the workspace lifecycle lock, open the request (which is where a missing or corrupt
# retention policy refuses), decide the audience and the purposes, resolve every
# reference under the caller, resolve the mentions, and only then write. Nothing is
# inserted before every refusal that could happen has happened — which is what makes
# "refuses without a resulting record" a property of the ordering rather than of a
# rollback.
#
# **The lock comes before the first source read, and that is § A7 rather than caution.**
# Both of these are provenance writers: they read a source's eligibility and then store
# a link asserting the memory they write was derived from it. Under ``READ COMMITTED`` a
# source read issued before the lock sees whatever was committed when it ran, so a
# concurrent correction, supersession or erasure of that source could commit between the
# read and the insert and leave a memory derived from a row that is no longer current —
# a dependency the closure that invalidated it has already walked past. Locking first
# makes the reread after the wait the contract's own "a writer that was waiting must
# reread eligibility and revision after the lock". The lock is transaction-scoped and
# re-entrant, so the trusted source seam taking it again inside ``write_memory``'s
# caller costs nothing.


def remember(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: RememberInput
) -> MemoryWritten:
    """Record a memory somebody stated, with its own non-memory provenance.

    ``origin`` is ``told``, ``recorded_at`` is this request's frozen instant and
    ``recorded_by`` is the context's actor: all three are server-owned, and none of
    them is a field the input model has.

    Explicit statement is *evidence*, not a requirement — nothing in the design says a
    durable memory has to arrive through this operation. It is the manual path, and
    the trusted source seam is the other one.
    """
    consumers = consumers_for_dispatch(uow)
    lock_workspace_lifecycle(uow.connection)
    request = _opened(ctx, uow)
    audience = resolve_audience(ctx, model_input.audience)
    purposes = resolve_purposes(ctx, model_input.purposes)
    links = resolve_stated_links(
        ctx,
        uow,
        provenance_refs=model_input.provenance_refs,
        about_refs=model_input.about_refs,
        request=request,
    )
    mentions = resolve_mentions(
        ctx, uow, model_input.mentions, request=request, now=request.now
    )
    return write_memory(
        ctx,
        uow,
        kind=model_input.kind,
        title=model_input.title,
        body=model_input.body,
        audience=audience,
        purposes=purposes,
        confidence=model_input.confidence,
        occurred_at=model_input.occurred_at,
        origin=ORIGIN_TOLD,
        recorded_at=request.now,
        recorded_by_kind=ctx.actor.kind.value,
        recorded_by_id=ctx.actor.id,
        links=links,
        mentions=mentions,
        consumers=consumers,
    )


def derive(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: DeriveInput
) -> MemoryWritten:
    """Record a memory computed from 1-64 distinct, currently authorized sources.

    The sources decide two things the caller cannot widen: the audience meet and the
    purpose intersection. Both are computed **before** any write, so a derivation
    whose sources meet nowhere (``audience_empty``) or share no purpose
    (``purposes_empty``) leaves no memory, no entity and no link behind.

    A derivation cannot select ``producer_kind``, an authority, source keys, worker
    authority or source times — not by refusal but by construction: :class:`DeriveInput`
    has no field for any of them, and :func:`~rheo_recallatron.writes.write_memory`
    takes the provenance from this call site, which passes none.
    """
    consumers = consumers_for_dispatch(uow)
    lock_workspace_lifecycle(uow.connection)
    request = _opened(ctx, uow)
    resolved = resolve_sources(
        ctx,
        uow,
        model_input.sources,
        about_refs=model_input.about_refs,
        request=request,
    )
    audience = resolve_audience(ctx, model_input.audience, meet=resolved.audience)
    purposes = resolve_purposes(ctx, model_input.purposes, available=resolved.purposes)
    mentions = resolve_mentions(
        ctx, uow, model_input.mentions, request=request, now=request.now
    )
    return write_memory(
        ctx,
        uow,
        kind=model_input.kind,
        title=model_input.title,
        body=model_input.body,
        audience=audience,
        purposes=purposes,
        confidence=model_input.confidence,
        occurred_at=model_input.occurred_at,
        origin=ORIGIN_DERIVED,
        recorded_at=request.now,
        recorded_by_kind=ctx.actor.kind.value,
        recorded_by_id=ctx.actor.id,
        links=resolved.links,
        mentions=mentions,
        consumers=consumers,
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

REMEMBER_DECLARATION: Final = OperationDeclaration(
    name=MEMORY_REMEMBER,
    safety_class=SafetyClass.MUTATE,
    roles=WRITE_ROLES,
    input_model=RememberInput,
    output=MemoryWritten,
    # Not ``NATURAL``: there is no unique index that makes a repeat the same row, and
    # there must not be — two people writing the same sentence twice recorded two
    # things, and the one identity-keyed write path in this module is the trusted
    # source seam, whose key is the receipt's rather than the memory's content.
    idempotency=Idempotency.NONE,
    # ``subject_field=None``: an ``AuditSpec`` subject names an input field carrying a
    # ``RecordRef`` for the record the call acts *on*, and a write that creates a
    # record acts on none — the reference does not exist until the handler mints it.
    # The audit row still lands, with a null subject and the request digest that
    # distinguishes it from every other call.
    audit=AuditSpec(subject_field=None),
)

DERIVE_DECLARATION: Final = OperationDeclaration(
    name=MEMORY_DERIVE,
    safety_class=SafetyClass.MUTATE,
    roles=WRITE_ROLES,
    input_model=DeriveInput,
    output=MemoryWritten,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)

CORRECT_DECLARATION: Final = OperationDeclaration(
    name=MEMORY_CORRECT,
    safety_class=SafetyClass.MUTATE,
    # No ``service``: § A10's table gives correcting and superseding to owner and
    # member alone. A service may write what it learns and may not rewrite what
    # somebody else recorded.
    roles=LIFECYCLE_ROLES,
    input_model=CorrectInput,
    output=MemoryCorrected,
    # Not ``NATURAL``: correcting twice with the same text is two corrections, and
    # the compare-and-set is what stops the second one acting on a stale copy.
    idempotency=Idempotency.NONE,
    # The first Recallatron declaration that names a subject field, because it is the
    # first that acts on a record that already exists. ``dispatch``'s ``_subject_ref``
    # reads it off the validated input, so the audit row for a correction names the
    # memory it corrected rather than carrying the null a creating write does.
    #
    # ``MUTATE`` attaches **no** execution guard — ``core_guards_for`` returns an
    # empty tuple below the three gated classes — and that is correct rather than a
    # gap. ``RecordStateGuard`` exists to recheck that an *approved* call's subject
    # has not moved between the approval and the effect; a mutate has no approval and
    # no such window, and its concurrency control is the ``expected_revision`` CAS in
    # this operation's own input, judged under the lifecycle lock.
    audit=AuditSpec(subject_field="ref"),
)

SUPERSEDE_DECLARATION: Final = OperationDeclaration(
    name=MEMORY_SUPERSEDE,
    safety_class=SafetyClass.MUTATE,
    roles=LIFECYCLE_ROLES,
    input_model=SupersedeInput,
    output=MemorySuperseded,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field="ref"),
)

OPERATIONS: Final[tuple[tuple[OperationDeclaration, Handler], ...]] = (
    (RECALL_DECLARATION, recall),
    (READ_DECLARATION, read),
    (REMEMBER_DECLARATION, remember),
    (DERIVE_DECLARATION, derive),
    (CORRECT_DECLARATION, correct),
    (SUPERSEDE_DECLARATION, supersede),
    *ENTITY_OPERATIONS,
    (DEDUP_DECLARATION, dedup_candidates),
    *EMBEDDING_OPERATIONS,
)
"""What the manifest declares: two reads, two writes, the two lifecycle changes, the
two service-only entity reads, the service-only dedup-candidate read and the owner's
embedding rebuild. The record resolver is declared beside this tuple
on the manifest and shares the same eligibility function; erasure is not here at all,
because a memory is erased through the core's own record-delete operation against the
owned-delete pair this module declares on its record type."""
