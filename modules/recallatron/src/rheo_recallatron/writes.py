"""The write machinery every path into ``recallatron.memory`` shares: the audience
ceiling, the purpose rule, reference resolution, and the one row insert.

**Three callers, one implementation.** ``remember`` and ``derive`` are the two
operations; trusted acceptance is the third, and it has to write a memory the same way
they do or the restrictions an automatic memory carries would be a second set of rules
nobody reviewed. So the audience narrowing, the purpose set, the link and mention
writes, the row insert and the ``recorded`` publish all live here, and each caller
supplies the origin, the clock and the provenance that make its path different.

**§ A4's two rules, stated once because they are easy to state twice and wrongly.**

*Audience.* Accountless contexts create workspace memory only; a bound delegated
caller with an account has that account's member ceiling and asking for anything wider
is ``audience_unavailable``; an unbound account caller may choose either and gets the
workspace audience when it says nothing. A derivation additionally narrows to its
sources' meet — silently when the caller named no audience, and as a refusal when it
named one the meet does not admit, because a caller told "stored" after asking for a
workspace audience would believe it had shared something it had not.

*Purpose.* A bound context's writes carry exactly its binding; an explicit set holding
anything else is ``purpose_mismatch``, and a derivation intersection that excludes the
binding is ``purposes_empty``. An unbound caller supplies its own nonempty set, bounded
by the intersection where there is one. There is no guessed default: a memory with no
purpose would be a memory no bound read could ever return.

**Nothing here decides who may *read* anything.** Source eligibility is
:func:`~rheo_recallatron.eligibility.eligible_memory`'s, and a non-memory reference is
the core resolver's. This file consumes those answers; it does not second-guess them.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from rheo_contracts import ContextPurpose, RecordRef, WorkspaceContext
from rheo_core.events.consumers import ConsumerRegistry
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork

from rheo_recallatron.configuration import MAX_REFS_PER_WRITE
from rheo_recallatron.contracts import EntityHead, MemoryWritten
from rheo_recallatron.eligibility import (
    AUDIENCE_MEMBER,
    AUDIENCE_WORKSPACE,
    RELATION_ABOUT,
    RELATION_DERIVED_FROM,
    Denied,
    MemoryRequest,
    ReadMode,
    begin_request,
    eligible_memory,
    memory_reference,
)
from rheo_recallatron.embedding.enqueue import enqueue_embed_job
from rheo_recallatron.entities import ResolvedMention, readable_ref, write_mentions
from rheo_recallatron.events import MEMORY_RECORDED, publish_memory_event
from rheo_recallatron.references import canonical_ref, entity_reference, is_memory_ref
from rheo_recallatron.refusals import (
    AUDIENCE_EMPTY,
    AUDIENCE_UNAVAILABLE,
    INPUT_INVALID,
    NOT_FOUND,
    PURPOSE_MISMATCH,
    PURPOSES_EMPTY,
    REFERENCE_SCAN_LIMIT,
    RETENTION_UNAVAILABLE,
    USE_DERIVE,
)
from rheo_recallatron.storage.repository import (
    MemoryLinkRow,
    MemoryPurposeRow,
    MemoryRow,
    insert_memory,
    insert_memory_link,
    insert_memory_purpose,
    list_memory_links,
    list_memory_purposes,
)

ORIGIN_TOLD: Final = "told"
ORIGIN_DERIVED: Final = "derived"
"""§ A3's two origins a 1a1 writer produces. ``migrated`` belongs to the import path.

An automatic memory keeps ``derived``: its provenance is distinguished by the
receipt's ``producer_kind``, not by a fourth origin, so nothing downstream has to
learn a new vocabulary to tell one apart.
"""

ALL_PURPOSES: Final = frozenset(ContextPurpose)


@dataclass(frozen=True, slots=True)
class WriteAudience:
    """A resolved audience: the kind, and the account id a member audience carries.

    ``id`` is null exactly for ``workspace``, which is the pairing the database's own
    check constraint enforces — so an inconsistent value here fails the insert rather
    than being stored.
    """

    kind: str
    id: UUID | None


WORKSPACE_AUDIENCE: Final = WriteAudience(AUDIENCE_WORKSPACE, None)


@dataclass(frozen=True, slots=True)
class ProposedLink:
    """One link a write proposes, before it is deduplicated.

    ``supersession_lineage`` defaults to ``False``, which is what every caller-facing
    path produces: an ordinary link is permission-bearing, and a caller cannot ask
    for a marked one. Only the supersession closure sets it — for the ancestry it
    copies off a predecessor and for the one marked ``derived_from`` it adds to that
    predecessor — so "the marker is not accepted in caller input" (§ A5) is a
    property of who can reach this field rather than of a refusal.
    """

    ref: str
    relation: str
    supersession_lineage: bool = False


def opened(ctx: WorkspaceContext, uow: UnitOfWork) -> MemoryRequest:
    """Freeze the clock, the retention window and the shared reference budget.

    Called **first** on every write path, exactly as it is on every read path: a
    workspace whose retention policy is missing, unparseable or out of range refuses
    ``retention_unavailable`` before a row, an entity or a link is written. Fail
    closed — the package default is what *enable* writes, never what a writer
    substitutes for a policy row somebody deleted (AC 8).
    """
    request = begin_request(ctx, uow)
    if request is None:
        raise OperationRefused(
            RETENTION_UNAVAILABLE, "this workspace has no usable retention policy"
        )
    return request


# --- audience -------------------------------------------------------------------------


def _meet(left: WriteAudience, right: WriteAudience) -> WriteAudience | None:
    """The narrower of two audiences, or ``None`` when they meet nowhere.

    Two different member audiences have no common audience at all, which is the case
    § A4 calls ``audience_empty``: there is no third party both members belong to.
    """
    if left.kind == AUDIENCE_WORKSPACE:
        return right
    if right.kind == AUDIENCE_WORKSPACE:
        return left
    return left if left.id == right.id else None


def _allowed(ctx: WorkspaceContext) -> tuple[frozenset[str], str]:
    """Which audiences this caller may write, and which it gets by default."""
    account = ctx.principal.account_id
    if account is None:
        return frozenset({AUDIENCE_WORKSPACE}), AUDIENCE_WORKSPACE
    if ctx.principal.bound_purpose is not None:
        # Delegated work carries that account's member ceiling, and the ceiling is
        # also the default: bound work that says nothing keeps its narrowest reach.
        return frozenset({AUDIENCE_MEMBER}), AUDIENCE_MEMBER
    return frozenset({AUDIENCE_WORKSPACE, AUDIENCE_MEMBER}), AUDIENCE_WORKSPACE


def resolve_audience(
    ctx: WorkspaceContext, requested: str | None, *, meet: WriteAudience | None = None
) -> WriteAudience:
    """The audience a write stores, or the refusal that stops it before any write."""
    allowed, default = _allowed(ctx)
    chosen = default if requested is None else requested
    if chosen not in allowed:
        raise OperationRefused(
            AUDIENCE_UNAVAILABLE, "that audience is outside this caller's ceiling"
        )
    candidate = WriteAudience(
        chosen, ctx.principal.account_id if chosen == AUDIENCE_MEMBER else None
    )
    if meet is None:
        return candidate
    narrowed = _meet(candidate, meet)
    if narrowed is None:
        raise OperationRefused(
            AUDIENCE_EMPTY, "these sources have no audience in common"
        )
    if requested is not None and narrowed != candidate:
        raise OperationRefused(
            AUDIENCE_UNAVAILABLE, "that audience is wider than these sources allow"
        )
    return narrowed


# --- purpose --------------------------------------------------------------------------


def resolve_purposes(
    ctx: WorkspaceContext,
    requested: frozenset[ContextPurpose] | None,
    *,
    available: frozenset[ContextPurpose] | None = None,
) -> tuple[str, ...]:
    """§ A4's write rule, over an optional derivation intersection.

    ``available`` is the intersection a derivation computed, or ``None`` for a write
    with no sources to intersect — for which every declared purpose is available. The
    order of the two refusals matters and is § A4's own: an explicit set holding a
    purpose other than the binding is ``purpose_mismatch`` (the *request* is wrong)
    before an intersection that excludes the binding is ``purposes_empty`` (the
    *sources* are).
    """
    bound = ctx.principal.bound_purpose
    if requested is not None and not requested:
        raise OperationRefused(PURPOSES_EMPTY, "a memory carries at least one purpose")
    if bound is not None:
        if requested is not None and requested != {bound}:
            raise OperationRefused(
                PURPOSE_MISMATCH, "a bound caller writes exactly its bound purpose"
            )
        if available is not None and bound not in available:
            raise OperationRefused(
                PURPOSES_EMPTY, "these sources do not carry this caller's purpose"
            )
        return (bound.value,)
    if requested is None:
        if available is None:
            raise OperationRefused(
                PURPOSES_EMPTY, "an unbound caller states the purposes it is writing"
            )
        chosen = available
    else:
        chosen = frozenset(requested)
        if available is not None and not chosen <= available:
            raise OperationRefused(
                PURPOSES_EMPTY, "these sources do not carry every stated purpose"
            )
    if not chosen:
        raise OperationRefused(PURPOSES_EMPTY, "these sources share no purpose")
    return tuple(sorted(purpose.value for purpose in chosen))


def checked_purpose(ctx: WorkspaceContext, stated: str | None) -> None:
    """The read rule: a stated purpose is checked against the binding, never adopted.

    Here beside the write rule, and shared the way :func:`opened` is, because every
    read operation runs it and ``dedup_candidates`` cannot import the operations
    module that registers it. One function, so ``input_invalid`` and
    ``purpose_mismatch`` cannot come to mean different things on different reads.

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


# --- references -----------------------------------------------------------------------


def _charge(request: MemoryRequest, reference: str) -> None:
    """Spend one reference from the request's shared budget, or refuse.

    § A13: a derive validates the complete proposed copied graph against the read
    budgets **before committing**, and refuses ``reference_scan_limit`` if the result
    would be unreadable under them. Charging each proposed link here is that check —
    the write is bounded by exactly the allowance a later read of it will have.
    """
    if not request.budget.charge(reference):
        raise OperationRefused(
            REFERENCE_SCAN_LIMIT, "this write exceeded its reference budget"
        )


def _readable_record(ctx: WorkspaceContext, uow: UnitOfWork, parsed: RecordRef) -> None:
    """A non-memory reference has to resolve live and readable under this caller.

    The same resolution the entity service applies to a backing ref, reused rather
    than restated: one answer to "may this caller see the record this reference
    names", whichever write path is asking.
    """
    if not readable_ref(ctx, uow, parsed.format()):
        raise OperationRefused(NOT_FOUND, "no such record")


def resolve_stated_links(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    *,
    provenance_refs: tuple[str, ...],
    about_refs: tuple[str, ...],
    request: MemoryRequest,
) -> tuple[ProposedLink, ...]:
    """``remember``'s own references: canonical, non-memory, and readable.

    Every ``recallatron.memory`` reference in either relation refuses ``use_derive``
    **before a row or an entity is created**. A memory built from other memories is a
    derivation, and derivation is what computes the audience meet and the purpose
    intersection; accepting the reference here would store a memory whose restrictions
    were never narrowed by the sources it came from.
    """
    proposed: list[ProposedLink] = []
    for reference, relation in (
        *((value, RELATION_DERIVED_FROM) for value in provenance_refs),
        *((value, RELATION_ABOUT) for value in about_refs),
    ):
        parsed = canonical_ref(reference)
        if is_memory_ref(parsed):
            raise OperationRefused(
                USE_DERIVE, "a memory reference belongs to a derivation"
            )
        _charge(request, parsed.format())
        _readable_record(ctx, uow, parsed)
        proposed.append(ProposedLink(parsed.format(), relation))
    if len(proposed) > MAX_REFS_PER_WRITE:
        raise OperationRefused(INPUT_INVALID, "too many references for one write")
    return tuple(proposed)


@dataclass(frozen=True, slots=True)
class DerivedSources:
    """What a derivation's sources decided, before any write happens."""

    audience: WriteAudience
    purposes: frozenset[ContextPurpose]
    links: tuple[ProposedLink, ...]


def resolve_sources(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    sources: tuple[str, ...],
    *,
    about_refs: tuple[str, ...],
    request: MemoryRequest,
) -> DerivedSources:
    """Resolve a derivation's sources and compute what they permit.

    Memory sources are authorized through the one eligibility function, in ``current``
    mode — a derivation's source must be a live row, not a retained history entry.
    Non-memory authoritative records resolve through the core resolver and contribute
    the workspace audience and the full purpose vocabulary, subject to their own
    resolvers: they carry no memory audience or purpose set of their own to narrow by.

    Every direct source becomes a stored ``derived_from`` link, and each memory
    source's own actual permission-bearing links are copied onto the result — § A5's
    "inherited actual source/about restrictions". Marked lineage rows are **not**
    copied: they carry ancestry rather than permission, and re-marking them on a fresh
    row would manufacture ancestry the new row does not have.
    """
    audience: WriteAudience = WORKSPACE_AUDIENCE
    purposes = ALL_PURPOSES
    proposed: list[ProposedLink] = []
    seen: set[str] = set()
    for reference in sources:
        parsed = canonical_ref(reference)
        formatted = parsed.format()
        if formatted in seen:
            # § A13: duplicates collapse by canonical key before writing, so two
            # spellings of one reference are one source rather than two that inflate
            # the 1-64 count.
            continue
        seen.add(formatted)
        _charge(request, formatted)
        if not is_memory_ref(parsed):
            _readable_record(ctx, uow, parsed)
            proposed.append(ProposedLink(formatted, RELATION_DERIVED_FROM))
            continue
        decision = eligible_memory(
            ctx, uow, parsed.id, mode=ReadMode.CURRENT, request=request
        )
        if isinstance(decision, Denied):
            if decision.state == REFERENCE_SCAN_LIMIT:
                raise OperationRefused(
                    REFERENCE_SCAN_LIMIT, "this write exceeded its reference budget"
                )
            raise OperationRefused(NOT_FOUND, "no such memory")
        row = decision.row
        met = _meet(
            audience,
            WriteAudience(row.audience_kind, row.audience_id),
        )
        if met is None:
            raise OperationRefused(
                AUDIENCE_EMPTY, "these sources have no audience in common"
            )
        audience = met
        purposes &= {
            ContextPurpose(value)
            for value in list_memory_purposes(uow.connection, row.id)
        }
        proposed.append(ProposedLink(formatted, RELATION_DERIVED_FROM))
        for link in list_memory_links(uow.connection, row.id):
            if link.supersession_lineage:
                continue
            _charge(request, link.ref)
            proposed.append(ProposedLink(link.ref, link.relation))
    for reference in about_refs:
        parsed = canonical_ref(reference)
        if is_memory_ref(parsed):
            raise OperationRefused(
                USE_DERIVE, "a memory reference belongs in sources, not about"
            )
        _charge(request, parsed.format())
        _readable_record(ctx, uow, parsed)
        proposed.append(ProposedLink(parsed.format(), RELATION_ABOUT))
    return DerivedSources(audience, purposes, tuple(proposed))


def _deduplicated(links: tuple[ProposedLink, ...]) -> tuple[ProposedLink, ...]:
    """One row per ``(ref, relation)``, in first-seen order.

    The composite primary key is ``(memory_id, ref, relation)``, so a duplicate is not
    a second link — it is the same link twice, and inserting it would raise rather
    than store anything new.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[ProposedLink] = []
    for link in links:
        key = (link.ref, link.relation)
        if key in seen:
            continue
        seen.add(key)
        unique.append(link)
    return tuple(unique)


# --- the one row insert ---------------------------------------------------------------


def write_memory(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    *,
    kind: str,
    title: str,
    body: str,
    audience: WriteAudience,
    purposes: tuple[str, ...],
    confidence: float | None,
    occurred_at: datetime | None,
    origin: str,
    recorded_at: datetime,
    recorded_by_kind: str,
    recorded_by_id: UUID | None,
    links: tuple[ProposedLink, ...],
    mentions: tuple[ResolvedMention, ...],
    consumers: ConsumerRegistry,
    source_namespace: str | None = None,
    external_source_key: str | None = None,
) -> MemoryWritten:
    """Insert the memory, its purposes, mentions and links, and publish ``.recorded``.

    All of it in the caller's one transaction, and the publish **inside** it: the
    outbox row and the row it describes commit together or neither does. The
    dispatcher's success audit row is written last, in the same transaction, which is
    what makes the three-way atomicity AC 1 asks for a property of the transaction
    rather than of three cooperating writers.

    ``origin``, ``recorded_at`` and ``recorded_by`` are parameters rather than read
    here because they are the only things that differ between a stated memory, a
    derivation and a trusted acceptance — and each of the three is server-owned at its
    own call site. No caller-supplied value reaches any of them.

    A mention naming a backing ref also stores an ``about`` link to that ref in this
    same transaction (§ A3), so the entity's independent route is a permission-bearing
    link on the memory rather than a claim only the entity row remembers.
    """
    memory_id = uuid7()
    insert_memory(
        uow.connection,
        MemoryRow(
            id=memory_id,
            kind=kind,
            title=title,
            body=body,
            audience_kind=audience.kind,
            audience_id=audience.id,
            confidence=confidence,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            recorded_by_kind=recorded_by_kind,
            recorded_by_id=recorded_by_id,
            origin=origin,
            revision=1,
            corrected_at=None,
            superseded_by_id=None,
            invalidated_at=None,
            invalidation_reason=None,
            source_namespace=source_namespace,
            external_source_key=external_source_key,
        ),
    )
    # The embed job, when the workspace can use one: it commits with this row, and the
    # provider runs in the worker afterwards. ``correct`` calls the same helper itself,
    # because it rewrites a row in place and never reaches this insert.
    enqueue_embed_job(ctx, uow, memory_id=memory_id, now=recorded_at)
    for purpose in purposes:
        insert_memory_purpose(
            uow.connection, MemoryPurposeRow(memory_id=memory_id, purpose=purpose)
        )
    write_mentions(uow.connection, memory_id, mentions)
    backing = tuple(
        ProposedLink(mention.backing_ref, RELATION_ABOUT)
        for mention in mentions
        if mention.backing_ref is not None
    )
    for link in _deduplicated(links + backing):
        insert_memory_link(
            uow.connection,
            MemoryLinkRow(
                memory_id=memory_id,
                ref=link.ref,
                relation=link.relation,
                created_at=recorded_at,
                supersession_lineage=link.supersession_lineage,
            ),
        )
    reference = memory_reference(memory_id)
    publish_memory_event(
        ctx,
        uow,
        event_type=MEMORY_RECORDED,
        memory_ref=reference,
        kind=kind,
        reason=None,
        revision=1,
        consumers=consumers,
    )
    return MemoryWritten(
        ref=reference,
        kind=kind,
        audience=audience.kind,
        purposes=purposes,
        recorded_at=recorded_at,
        revision=1,
        mentions=tuple(
            EntityHead(
                ref=entity_reference(mention.entity_id),
                kind=mention.kind,
                name=mention.name,
                backing_ref=mention.backing_ref,
                role=mention.role,
            )
            for mention in mentions
        ),
    )
