"""Entities: non-probing creation, authorized selection, bounded visibility, and the
prune that follows a last mention out of the transaction that removed it.

**There is no name probe anywhere in this file, and that is the design.** § A5 ratifies
creation as "always generates a fresh UUID, including hidden same-name entities", and
§ A3 puts *no* uniqueness constraint on ``name`` or ``normalized_name``. A create that
matched on name would answer "does an entity called this already exist in this
workspace" — across every audience boundary at once — to anybody who can write a
memory. Two entities with the same normalized name are two entities; the nonunique
``(kind, normalized_name, id)`` index exists to filter a caller's *already eligible*
set, never to answer existence.

**Visibility has exactly two routes, and no third.** An entity is reachable when a
memory that mentions it is eligible to this caller, or when its immutable backing ref
resolves independently for this caller. No route is the same ``not_found`` as an
unknown id: no hidden label, ref, collision or count comes back, and a caller cannot
tell "exists, not yours" from "never existed".

**Counts come from eligible results only** (§ A13). ``mention_count`` is the number of
mentions this caller can actually see, and there is no total beside it: a count that
included memories the caller may not read would report their existence.
"""

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from rheo_contracts import (
    Idempotency,
    OperationDeclaration,
    RecordRef,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import LIVE, Unavailable, UnitOfWork, resolve_in
from sqlalchemy import ColumnElement, Connection, delete, literal, select

from rheo_recallatron.configuration import CANDIDATE_SCAN_LIMIT, MODULE_ID
from rheo_recallatron.contracts import (
    EntityGetInput,
    EntityItem,
    EntityList,
    EntityListInput,
    MentionCreate,
    MentionInput,
    MentionSelect,
)
from rheo_recallatron.eligibility import (
    READ_ROLES,
    Denied,
    MemoryRequest,
    ReadMode,
    begin_request,
    eligible_memory,
    row_local_conditions,
)
from rheo_recallatron.references import canonical_ref, entity_reference, entity_target
from rheo_recallatron.refusals import (
    INPUT_INVALID,
    NOT_FOUND,
    REFERENCE_SCAN_LIMIT,
    RETENTION_UNAVAILABLE,
)
from rheo_recallatron.storage import tables as t
from rheo_recallatron.storage.repository import (
    MemoryEntityRow,
    MemoryMentionRow,
    get_memory_entity,
    insert_memory_entity,
    insert_memory_mention,
)

ENTITY_LIST: Final = f"{MODULE_ID}.entity.list"
ENTITY_GET: Final = f"{MODULE_ID}.entity.get"


def normalized_name(name: str) -> str:
    """The deterministic normalized form § A3 requires of every entity.

    NFKC so two Unicode spellings of one string normalize together, case-folded
    (which is not the same as lowercasing for every script), and internal whitespace
    collapsed. Deterministic is the whole requirement: the column exists to make a
    caller's own eligible set filterable, and it never decides identity — creation
    mints a fresh UUID whatever this returns.
    """
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


# --- visibility -----------------------------------------------------------------------


def readable_ref(ctx: WorkspaceContext, uow: UnitOfWork, reference: str) -> bool:
    """Whether a backing reference resolves live and readable for this caller.

    The core's own resolver, reused rather than reimplemented: it is what knows
    whether the owning module is enabled, whether the row is live, and whether this
    caller may read it. A reference that will not even parse is treated as no route
    rather than raised on — a stored value this process cannot parse is a route
    nobody can check, so it fails closed.
    """
    try:
        parsed = RecordRef.parse(reference)
    except Exception:
        return False
    resolved = resolve_in(parsed, ctx, uow)
    if isinstance(resolved, Unavailable):
        return False
    return resolved.readable and resolved.state == LIVE


def _mentioning_memories(
    uow: UnitOfWork, entity_id: UUID, request: MemoryRequest
) -> tuple[UUID, ...]:
    """At most :data:`CANDIDATE_SCAN_LIMIT` memories mentioning this entity, ordered.

    Prefiltered row-locally by the same conditions every read uses, so audience,
    purpose, retention and lifecycle narrow in SQL and full eligibility decides in
    Python — the split ``eligibility.py`` documents. Bounded for the reason every
    other scan in this module is: an entity mentioned by a hundred thousand memories
    must not make one visibility question unbounded work.
    """
    statement = (
        select(t.memory.c.id)
        .where(
            select(literal(1))
            .where(
                t.memory_mention.c.memory_id == t.memory.c.id,
                t.memory_mention.c.entity_id == entity_id,
            )
            .exists(),
            *row_local_conditions(request, ReadMode.CURRENT),
        )
        .order_by(t.memory.c.recorded_at, t.memory.c.id)
        .limit(CANDIDATE_SCAN_LIMIT)
    )
    return tuple(row[0] for row in uow.connection.execute(statement).all())


@dataclass(frozen=True, slots=True)
class VisibleEntity:
    """An entity this caller may see, and how many of its mentions it may see."""

    row: MemoryEntityRow
    mention_count: int


def visible_entity(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    entity_id: UUID,
    *,
    request: MemoryRequest,
) -> VisibleEntity | Denied:
    """§ A5's two routes, evaluated in full, or ``not_found``.

    The eligible mention count is computed whichever route granted visibility, so a
    caller reached through a backing ref is told how many mentions **it** can see
    rather than how many exist.
    """
    row = get_memory_entity(uow.connection, entity_id)
    if row is None:
        return Denied(NOT_FOUND)
    count = 0
    for memory_id in _mentioning_memories(uow, entity_id, request):
        decision = eligible_memory(
            ctx, uow, memory_id, mode=ReadMode.CURRENT, request=request
        )
        if isinstance(decision, Denied):
            if decision.state == REFERENCE_SCAN_LIMIT:
                return decision
            continue
        count += 1
    if count == 0 and not (row.ref is not None and readable_ref(ctx, uow, row.ref)):
        return Denied(NOT_FOUND)
    return VisibleEntity(row, count)


def _item(visible: VisibleEntity) -> EntityItem:
    return EntityItem(
        ref=entity_reference(visible.row.id),
        kind=visible.row.kind,
        name=visible.row.name,
        backing_ref=visible.row.ref,
        mention_count=visible.mention_count,
    )


# --- writing: creation, selection, collapsing and pruning ----------------------------


@dataclass(frozen=True, slots=True)
class ResolvedMention:
    """One mention as it will be written, with the entity it names already decided."""

    entity_id: UUID
    role: str | None
    kind: str
    name: str
    backing_ref: str | None


def create_entity(
    conn: Connection,
    *,
    kind: str,
    name: str,
    backing_ref: str | None,
    now: datetime,
) -> MemoryEntityRow:
    """A fresh entity, every time, with no lookup of any kind before the insert."""
    return insert_memory_entity(
        conn,
        MemoryEntityRow(
            id=uuid7(),
            kind=kind,
            name=name,
            normalized_name=normalized_name(name),
            ref=backing_ref,
            created_at=now,
            source_namespace=None,
            external_source_key=None,
        ),
    )


def resolve_mentions(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    mentions: tuple[MentionInput, ...],
    *,
    request: MemoryRequest,
    now: datetime,
) -> tuple[ResolvedMention, ...]:
    """Turn the typed mention union into the rows a write will insert.

    **Selections are collapsed and creations are not**, because the composite key is
    ``(memory_id, entity_id)`` and a creation has no entity id until it is minted:
    two ``create`` entries naming the same thing are two entities by design, while two
    ``select`` entries naming one entity are one mention. Two selections of the same
    entity that disagree about ``role`` are a contradictory variant of one mention and
    refuse ``input_invalid`` — one row cannot carry two roles, and silently keeping
    either would be a coin flip a caller could not see.

    Creation happens **after** every selection has been authorized, so a mixed input
    whose selection is not this caller's leaves no orphan entity behind.
    """
    selected: dict[UUID, ResolvedMention] = {}
    creations: list[MentionCreate] = []
    for mention in mentions:
        if isinstance(mention, MentionSelect):
            entity_id = entity_target(mention.entity_ref)
            decision = visible_entity(ctx, uow, entity_id, request=request)
            if isinstance(decision, Denied):
                raise _denied(decision)
            already = selected.get(entity_id)
            if already is not None:
                if already.role != mention.role:
                    raise OperationRefused(
                        INPUT_INVALID,
                        "one entity is mentioned twice with different roles",
                    )
                continue
            selected[entity_id] = ResolvedMention(
                entity_id=entity_id,
                role=mention.role,
                kind=decision.row.kind,
                name=decision.row.name,
                backing_ref=decision.row.ref,
            )
            continue
        creations.append(mention)

    resolved = list(selected.values())
    for creation in creations:
        if creation.backing_ref is not None:
            # "A backing ref must resolve before its label is returned" — and before
            # the entity exists at all, so an unreadable one creates nothing.
            canonical_ref(creation.backing_ref)
            if not readable_ref(ctx, uow, creation.backing_ref):
                raise OperationRefused(NOT_FOUND, "no such record")
        row = create_entity(
            uow.connection,
            kind=creation.kind,
            name=creation.name,
            backing_ref=creation.backing_ref,
            now=now,
        )
        resolved.append(
            ResolvedMention(
                entity_id=row.id,
                role=creation.role,
                kind=row.kind,
                name=row.name,
                backing_ref=row.ref,
            )
        )
    return tuple(resolved)


def write_mentions(
    conn: Connection, memory_id: UUID, mentions: tuple[ResolvedMention, ...]
) -> None:
    for mention in mentions:
        insert_memory_mention(
            conn,
            MemoryMentionRow(
                memory_id=memory_id, entity_id=mention.entity_id, role=mention.role
            ),
        )


def remove_mention(conn: Connection, memory_id: UUID, entity_id: UUID) -> bool:
    """Remove one mention and prune the entity when it was the last one.

    Answers whether the entity was pruned. § A5: "removing the last mention prunes an
    entity without a backing ref **in that transaction**" — so the prune is here,
    inside the caller's own unit of work, rather than left to a sweep that would leave
    an unreferenced entity addressable in between.

    An entity with a backing ref survives its last mention: the ref is an independent
    route to it, and pruning a row somebody else's record still points at would make
    that reference dangle.
    """
    conn.execute(
        delete(t.memory_mention).where(
            t.memory_mention.c.memory_id == memory_id,
            t.memory_mention.c.entity_id == entity_id,
        )
    )
    row = get_memory_entity(conn, entity_id)
    if row is None or row.ref is not None:
        return False
    remaining = conn.execute(
        select(literal(1))
        .select_from(t.memory_mention)
        .where(t.memory_mention.c.entity_id == entity_id)
        .limit(1)
    ).first()
    if remaining is not None:
        return False
    conn.execute(delete(t.memory_entity).where(t.memory_entity.c.id == entity_id))
    return True


# --- the two service-only read operations ---------------------------------------------


def _denied(denied: Denied) -> OperationRefused:
    if denied.state == REFERENCE_SCAN_LIMIT:
        return OperationRefused(
            REFERENCE_SCAN_LIMIT, "this read exceeded its reference budget"
        )
    return OperationRefused(NOT_FOUND, "no such entity")


def _opened(ctx: WorkspaceContext, uow: UnitOfWork) -> MemoryRequest:
    request = begin_request(ctx, uow)
    if request is None:
        raise OperationRefused(
            RETENTION_UNAVAILABLE, "this workspace has no usable retention policy"
        )
    return request


def entity_list(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: EntityListInput
) -> EntityList:
    """The entities this caller can reach through an eligible mention.

    **Enumeration follows the mention route only, and the omission is deliberate.**
    An entity whose *only* route is an independently readable backing ref — which is
    an entity that outlived its last mention — is reachable by
    :func:`entity_get`, which takes a reference the caller already holds. Listing it
    would mean walking every entity row in the workspace and resolving each one's
    backing ref, which is unbounded work in service of a case that has no way to
    surface a name the caller did not already have.
    """
    request = _opened(ctx, uow)
    reachable: list[ColumnElement[bool]] = [
        select(literal(1))
        .where(
            t.memory_mention.c.entity_id == t.memory_entity.c.id,
            t.memory_mention.c.memory_id == t.memory.c.id,
            *row_local_conditions(request, ReadMode.CURRENT),
        )
        .exists()
    ]
    if model_input.kind is not None:
        reachable.append(t.memory_entity.c.kind == model_input.kind)
    candidates = (
        select(t.memory_entity.c.id)
        .where(*reachable)
        .order_by(t.memory_entity.c.normalized_name, t.memory_entity.c.id)
        .limit(CANDIDATE_SCAN_LIMIT)
    )

    items: list[EntityItem] = []
    for row in uow.connection.execute(candidates).all():
        if len(items) == model_input.limit:
            break
        decision = visible_entity(ctx, uow, row[0], request=request)
        if isinstance(decision, Denied):
            if decision.state == REFERENCE_SCAN_LIMIT:
                raise _denied(decision)
            continue
        items.append(_item(decision))
    return EntityList(items=tuple(items))


def entity_get(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: EntityGetInput
) -> EntityItem:
    """One entity, by a reference this caller already holds, or ``not_found``."""
    request = _opened(ctx, uow)
    decision = visible_entity(
        ctx, uow, entity_target(model_input.entity_ref), request=request
    )
    if isinstance(decision, Denied):
        raise _denied(decision)
    return _item(decision)


ENTITY_LIST_DECLARATION: Final = OperationDeclaration(
    name=ENTITY_LIST,
    safety_class=SafetyClass.READ,
    roles=READ_ROLES,
    input_model=EntityListInput,
    output=EntityList,
    idempotency=Idempotency.NONE,
    audit=None,
)

ENTITY_GET_DECLARATION: Final = OperationDeclaration(
    name=ENTITY_GET,
    safety_class=SafetyClass.READ,
    roles=READ_ROLES,
    input_model=EntityGetInput,
    output=EntityItem,
    idempotency=Idempotency.NONE,
    audit=None,
)
"""Service-only in release one: § A10's table marks both with no MCP tool, and the
tool run registers none for them. They are operations a service or the interface can
call, not a surface a model can reach."""

ENTITY_OPERATIONS: Final = (
    (ENTITY_LIST_DECLARATION, entity_list),
    (ENTITY_GET_DECLARATION, entity_get),
)
