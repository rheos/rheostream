"""Architecture § A7's one closure algorithm, and the three entry points onto it.

Correction, supersession and user erasure differ in **what they do to the rows the
closure finds**, not in how they find them, so there is one traversal here
(:func:`dependent_closure`) and three dispositions over it. Writing three walks would
have been three chances for the erasure path and the correction path to disagree
about what "derived from" reaches.

**The lock comes first, and everything else is read after it.** Every one of these
entry points takes the workspace record-lifecycle advisory lock through the core
helper before it reads eligibility, a revision, or a link — and so do ``remember``
and ``derive``, which § A7 counts as provenance writers. Under ``READ COMMITTED`` a
statement issued before the lock sees whatever was committed when it ran, so a writer
that read first and locked second would decide against a snapshot the lock was
supposed to make current. The order is the guarantee: *a writer that was waiting
rereads eligibility and revision after the lock; a writer committed earlier is
visible to closure.*

**The compare-and-set is judged before the current-state test, and that order is a
contract rather than a convenience.** § A7: authorize with the history-capable rules,
then compare the supplied revision, and only then refuse a noncurrent row. The case
it is written for is the loser of a concurrent supersession: the winner has already
made the predecessor noncurrent *and* incremented its revision, and the loser must be
told ``record_stale`` — its copy has moved — rather than ``not_found``, which would
say the record is gone and give it nothing to retry against.

**What each disposition does** (§ A5's closure table, and this file implements it
verbatim):

- *Correct* rewrites the target's title, body and confidence in place, bumps its
  revision, stamps ``corrected_at``, drops its embeddings, and marks the affected
  closure ``source_corrected``.
- *Supersede* captures the predecessor's audience, purposes, links and the affected
  closure **before** creating the replacement — so the replacement, which carries a
  marked ``derived_from`` edge back to the predecessor, can never appear in its own
  closure — then creates the replacement, marks the captured rows
  ``source_superseded``, and finally marks the predecessor itself with its successor.
- *User erasure* physically removes the target and its closure, prunes each entity
  that loses its last mention, and terminalizes the external receipt of every row it
  removes. It is split across the two halves the deletion coordinator calls: the
  owner's ``delete_owned`` takes the target, and this module's participant takes the
  dependants. That is the split the ratified cascade describes — step 2 removes the
  record and its own children, step 3 runs the participants — and it is what makes
  the coordinator's union of removed identifiers observable with one real module
  instead of one synthetic one.

**``.invalidated`` is published for exactly the rows a closure marks non-erasure
invalidated, and for nothing else.** A physically removed row is covered by the
core's own record-deleted event from the coordinator, so the two never both fire for
one row. The publish goes through the module's single
:func:`~rheo_recallatron.events.publish_memory_event` with the declaration the
manifest already carries; there is no second publisher and no second event shape.

**"Newly" invalidated is load-bearing.** A row that already carries an invalidation
reason is left exactly as it is: not re-marked, not re-stamped, not re-published, and
its revision not bumped. Re-marking would rewrite the reason a reader relies on and
emit a second event for a state that did not change.
"""

from collections import deque
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from rheo_contracts import RecordRef, WorkspaceContext
from rheo_core.boundary.context import Refusal
from rheo_core.deletion import (
    NOTHING_REMOVED,
    DeleteAuthorization,
    RemovedMemories,
    lock_workspace_lifecycle,
)
from rheo_core.events.consumers import ConsumerRegistry
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import Connection

from rheo_recallatron.configuration import MEMORY_RECORD_TYPE, MODULE_ID
from rheo_recallatron.contracts import (
    CorrectInput,
    MemoryCorrected,
    MemorySuperseded,
    SupersedeInput,
)
from rheo_recallatron.eligibility import (
    RELATION_DERIVED_FROM,
    SOURCE_CORRECTED,
    SOURCE_SUPERSEDED,
    Denied,
    MemoryRequest,
    ReadMode,
    deletion_admits,
    eligible_memory,
    memory_reference,
)
from rheo_recallatron.entities import remove_mention
from rheo_recallatron.events import (
    MEMORY_INVALIDATED,
    consumers_for_dispatch,
    publish_memory_event,
)
from rheo_recallatron.references import canonical_ref
from rheo_recallatron.refusals import NOT_FOUND, RECORD_STALE, REFERENCE_SCAN_LIMIT
from rheo_recallatron.source_units import REPRESENTATION_MEMORY, STATE_ERASED
from rheo_recallatron.storage.repository import (
    MemoryRow,
    correct_memory,
    delete_memory,
    delete_memory_embeddings,
    get_memory,
    invalidate_memory,
    list_memory_dependents,
    list_memory_links,
    list_memory_mentions,
    list_memory_purposes,
    set_source_receipt_state,
)
from rheo_recallatron.writes import (
    ORIGIN_DERIVED,
    ProposedLink,
    WriteAudience,
    opened,
    write_memory,
)

DELETION_PARTICIPANT_TYPES: Final = (f"{MODULE_ID}.{MEMORY_RECORD_TYPE}",)
"""The qualified record types :func:`on_record_deleted` hooks.

Assembled from the module id and the record type rather than written out, which is
the spelling every qualified name in this package uses.

**One entry, and it is this module's own type**, which reads like a module hooking
itself and is not. The ratified cascade splits a deletion into the owner's own rows
(step 2) and the participants' (step 3), and ``recallatron.on_record_deleted``'s rule
— remove every memory linked to the reference and every memory derived from those —
is written for *any* deleted reference, not for a memory in particular. Release one
ships no other owned record type for it to hook; the day one arrives, it is added
here and the handler below needs no change, because it works from the reference and
never from the row behind it.
"""


# --- the shared traversal ------------------------------------------------------------


def dependent_closure(
    conn: Connection, target_id: UUID, *, include_marked: bool
) -> tuple[UUID, ...]:
    """Every memory that depends on ``target_id``, nearest first.

    § A5's traversal, which is one shape for correction, supersession and erasure
    alike: the **first** hop takes every memory linking to the target in either
    ordinary relation, and every hop after it follows ``derived_from`` alone. An
    ``about`` link is a statement that this memory is about that record; it is not a
    claim to have been built out of it, so it carries the closure one step and no
    further.

    ``include_marked`` decides whether marked supersession-lineage edges are
    followed. Erasure and the two non-erasure closures follow them — "copied ancestry
    keeps closure connected across missing middle rows", which is the whole reason a
    replacement copies its predecessor's ancestry. Scheduled expiry is the one entry
    that must not (§ A5: "a replacement reached only by ancestry survives on its own
    clock"), and it passes ``False``.

    **It reads the reference, never the row.** ``memory_link.ref`` is a value with no
    foreign key behind it, so these rows outlive the memory they name — which is what
    lets the deletion participant compute this closure *after* the coordinator's
    owner half has already removed the target.

    The target is never in its own closure, cycles terminate on the seen set, and the
    walk is deterministic because :func:`list_memory_dependents` is ordered.
    """
    seen: set[UUID] = {target_id}
    found: list[UUID] = []
    frontier: deque[tuple[UUID, bool]] = deque([(target_id, True)])
    while frontier:
        current, first_hop = frontier.popleft()
        for link in list_memory_dependents(conn, memory_reference(current)):
            if link.supersession_lineage and not include_marked:
                continue
            if not first_hop and link.relation != RELATION_DERIVED_FROM:
                continue
            if link.memory_id in seen:
                continue
            seen.add(link.memory_id)
            found.append(link.memory_id)
            frontier.append((link.memory_id, False))
    return tuple(found)


# --- authorization, the compare-and-set, and the current-state test -------------------


def _denied(denied: Denied) -> OperationRefused:
    if denied.state == REFERENCE_SCAN_LIMIT:
        return OperationRefused(
            REFERENCE_SCAN_LIMIT, "this request exceeded its reference budget"
        )
    return OperationRefused(NOT_FOUND, "no such memory")


def _target(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    ref: RecordRef,
    expected_revision: int,
    *,
    request: MemoryRequest,
) -> MemoryRow:
    """§ A7's three tests, in § A7's order, under a lock the caller already holds.

    Authorization is the one eligibility function in ``history`` mode, which is what
    "using the history-capable rules independently of the current-state test" means:
    a retained row is authorized here and refused by the *third* test, so the second
    test — the compare-and-set — gets to speak first.
    """
    decision = eligible_memory(ctx, uow, ref.id, mode=ReadMode.HISTORY, request=request)
    if isinstance(decision, Denied):
        raise _denied(decision)
    row = decision.row
    if row.revision != expected_revision:
        raise OperationRefused(
            RECORD_STALE, "that memory has moved since the revision you hold"
        )
    if row.invalidated_at is not None or row.superseded_by_id is not None:
        # The revision matched and the row is not current: refused with no write, and
        # with the same word a missing record gets, because "it is here but retired"
        # is a fact about the record rather than about the caller's copy.
        raise OperationRefused(NOT_FOUND, "no such memory")
    return row


# --- the non-erasure disposition ------------------------------------------------------


def _mark_invalidated(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    memory_id: UUID,
    *,
    reason: str,
    now: datetime,
    consumers: ConsumerRegistry,
    successor: UUID | None = None,
) -> int | None:
    """Mark one row non-erasure invalidated and publish it; ``None`` if it already was.

    Embeddings go first and in the same transaction as the mark, so there is no
    instant at which a vector outlives the row's claim to be current.
    """
    row = get_memory(uow.connection, memory_id)
    if row is None or row.invalidation_reason is not None:
        return None
    revision = row.revision + 1
    delete_memory_embeddings(uow.connection, memory_id)
    invalidate_memory(
        uow.connection,
        memory_id,
        reason=reason,
        invalidated_at=now,
        revision=revision,
        superseded_by_id=successor,
    )
    publish_memory_event(
        ctx,
        uow,
        event_type=MEMORY_INVALIDATED,
        memory_ref=memory_reference(memory_id),
        kind=row.kind,
        reason=reason,
        revision=revision,
        consumers=consumers,
    )
    return revision


def _invalidate_closure(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    affected: Sequence[UUID],
    *,
    reason: str,
    now: datetime,
    consumers: ConsumerRegistry,
) -> None:
    """Mark every newly affected row, in identifier order.

    Sorted rather than walked in traversal order, because § A7 asks for row locks in
    deterministic UUID order: two closures that overlap must take their rows the same
    way round or they can deadlock against each other.

    **Every newly invalidated derivative gains a revision**, on the supersession path
    as well as the correction path. § A7 states the rule in its correction paragraph
    and states only the predecessor's bump in its supersession steps; applying it to
    one and not the other would leave a row whose lifecycle state changed carrying the
    revision it had before, so a caller holding that revision could pass the
    compare-and-set against a row that has since been invalidated underneath it. The
    closure table treats the two entries identically — "mark affected rows non-erasure
    invalidated" — which is the reading taken here.
    """
    for memory_id in sorted(affected):
        _mark_invalidated(
            ctx,
            uow,
            memory_id,
            reason=reason,
            now=now,
            consumers=consumers,
        )


# --- correction -----------------------------------------------------------------------


def correct(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: CorrectInput
) -> MemoryCorrected:
    """Fix what a memory says, and invalidate everything built on what it said.

    The corrected row stays **current**: it is the same record, now saying something
    else, so nothing marks it invalidated and no ``.invalidated`` is published for
    it. Its derivatives are the rows whose content was computed from the old text,
    and those are marked ``source_corrected``.
    """
    consumers = consumers_for_dispatch(uow)
    lock_workspace_lifecycle(uow.connection)
    request = opened(ctx, uow)
    row = _target(
        ctx, uow, model_input.ref, model_input.expected_revision, request=request
    )
    affected = dependent_closure(uow.connection, row.id, include_marked=True)
    revision = row.revision + 1
    delete_memory_embeddings(uow.connection, row.id)
    correct_memory(
        uow.connection,
        row.id,
        title=model_input.title,
        body=model_input.body,
        confidence=model_input.confidence,
        revision=revision,
        corrected_at=request.now,
    )
    _invalidate_closure(
        ctx,
        uow,
        affected,
        reason=SOURCE_CORRECTED,
        now=request.now,
        consumers=consumers,
    )
    return MemoryCorrected(
        ref=memory_reference(row.id),
        kind=row.kind,
        revision=revision,
        corrected_at=request.now,
    )


# --- supersession ---------------------------------------------------------------------


def supersede(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: SupersedeInput
) -> MemorySuperseded:
    """Replace a memory with a fresh one, keeping the old one as retained history.

    The four steps are § A5's, in its order, and the order is what the case turns on:
    the closure is captured at step 2, **before** the replacement exists at step 3, so
    the marked ``derived_from`` edge the replacement carries back to its predecessor
    cannot put the replacement into the set the next line invalidates. Capturing after
    the insert would invalidate the replacement the instant it was written, and no
    single assertion about the predecessor would notice.
    """
    consumers = consumers_for_dispatch(uow)
    lock_workspace_lifecycle(uow.connection)
    request = opened(ctx, uow)
    conn = uow.connection
    # (1) authorization, compare-and-set, current state — and the capture.
    predecessor = _target(
        ctx, uow, model_input.ref, model_input.expected_revision, request=request
    )
    purposes = list_memory_purposes(conn, predecessor.id)
    inherited = tuple(
        ProposedLink(link.ref, link.relation, link.supersession_lineage)
        for link in list_memory_links(conn, predecessor.id)
    )
    # (2) the pre-existing derivative closure, before a replacement can join it.
    affected = dependent_closure(conn, predecessor.id, include_marked=True)
    # (3) the replacement: the predecessor's own audience and purposes (the meet of a
    # one-source derivation is that source), every restriction it carried, its
    # ancestry, and one new marked edge naming the predecessor itself.
    written = write_memory(
        ctx,
        uow,
        kind=model_input.kind,
        title=model_input.title,
        body=model_input.body,
        audience=WriteAudience(predecessor.audience_kind, predecessor.audience_id),
        purposes=purposes,
        confidence=model_input.confidence,
        occurred_at=model_input.occurred_at,
        origin=ORIGIN_DERIVED,
        recorded_at=request.now,
        recorded_by_kind=ctx.actor.kind.value,
        recorded_by_id=ctx.actor.id,
        links=(
            *inherited,
            ProposedLink(memory_reference(predecessor.id), RELATION_DERIVED_FROM, True),
        ),
        mentions=(),
        consumers=consumers,
    )
    _invalidate_closure(
        ctx,
        uow,
        affected,
        reason=SOURCE_SUPERSEDED,
        now=request.now,
        consumers=consumers,
    )
    # (4) the predecessor last, so its successor pointer names a row that exists.
    revision = _mark_invalidated(
        ctx,
        uow,
        predecessor.id,
        reason=SOURCE_SUPERSEDED,
        now=request.now,
        consumers=consumers,
        successor=canonical_ref(written.ref).id,
    )
    # Unreachable: ``_target`` refused a noncurrent predecessor three statements up,
    # and this transaction holds the lifecycle lock, so nothing can have invalidated
    # it in between. Asserted rather than defaulted, because a ``None`` here would
    # otherwise be reported to the caller as a revision.
    assert revision is not None, "the predecessor was invalidated under the lock"
    return MemorySuperseded(
        replacement=written,
        predecessor_ref=memory_reference(predecessor.id),
        predecessor_revision=revision,
    )


# --- user erasure: the owner's half and the participant's ----------------------------


def _unauthorized(ref: RecordRef) -> Refusal:
    """The owner's refusal, in the same word a read gets and with nothing else in it.

    ``not_found`` rather than a state of this module's own invention: a caller who
    may not delete a memory must not be able to tell "there is one and it is not
    yours" from "there is none", and the coordinator passes an owner's refusal
    through to that caller unchanged.
    """
    return Refusal(NOT_FOUND, f"{ref.format()} is not a memory this caller may delete")


def authorize_memory_delete(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> DeleteAuthorization | Refusal:
    """The owned-delete authorizer: a reference and a revision, or a refusal.

    Content-free by shape and by content: it answers
    :class:`~rheo_core.deletion.DeleteAuthorization`, which has nowhere to put a
    title or a body, and it reads nothing beyond what
    :func:`~rheo_recallatron.eligibility.deletion_admits` needs.

    **It permits an authorized retained or expired row**, which is § A7's wording and
    is what the scheduled sweep depends on: the rows retention wants gone are exactly
    the ones a read can no longer see. The approved deletion path does not reach them
    anyway — the core's record-state guard resolves the subject in ``current`` mode
    before the effect and refuses a row that no longer resolves — so the breadth here
    costs nothing on that path and is the whole authorization on the scheduled one.
    """
    if ref.module != MODULE_ID or ref.record_type != MEMORY_RECORD_TYPE:
        return _unauthorized(ref)
    row = get_memory(uow.connection, ref.id)
    if row is None or not deletion_admits(ctx, uow, row):
        return _unauthorized(ref)
    return DeleteAuthorization(ref=ref, revision=row.revision)


def _erase(conn: Connection, memory_ids: Iterable[UUID]) -> frozenset[UUID]:
    """Physically remove each memory, and everything the workspace holds because of it.

    Three things happen per row and the order is forced. The mentions go **first**,
    through the entity service's own :func:`~rheo_recallatron.entities.remove_mention`,
    because that is what prunes an entity losing its last mention — deleting the
    memory first would cascade the mention away and strand the entity with no mention
    and no prune. The receipt is terminalized **before** the row, while its source key
    is still readable off it, so a later replay of that identity cannot resurrect what
    somebody asked to have erased. The memory goes last, taking its purposes, links
    and embeddings with it through their own cascades.

    Only rows that were actually there are returned. The coordinator counts the union
    of these sets into the ledger, and a row already absent must not be counted as one
    this deletion removed.
    """
    removed: set[UUID] = set()
    for memory_id in sorted(memory_ids):
        row = get_memory(conn, memory_id)
        if row is None:
            continue
        for entity_id in list_memory_mentions(conn, memory_id):
            remove_mention(conn, memory_id, entity_id)
        if row.source_namespace is not None and row.external_source_key is not None:
            set_source_receipt_state(
                conn,
                REPRESENTATION_MEMORY,
                row.source_namespace,
                row.external_source_key,
                state=STATE_ERASED,
            )
        if delete_memory(conn, memory_id):
            removed.add(memory_id)
    return frozenset(removed)


def delete_owned_memory(
    ctx: WorkspaceContext, uow: UnitOfWork, authorization: DeleteAuthorization
) -> RemovedMemories:
    """The owner's half: the target row and the rows it owns, and nothing beyond it.

    The dependants are :func:`on_record_deleted`'s, which is the ratified split
    between the cascade's step 2 and its step 3 — and is why the ledger's participant
    list names this module rather than being empty on every memory deletion.

    It runs under the workspace lifecycle lock without taking one: both callers, the
    approved coordinator and the scheduled-expiry wrapper, take it immediately before
    the third authorization and hold it through the effect.
    """
    return RemovedMemories(_erase(uow.connection, (authorization.ref.id,)))


def on_record_deleted(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> RemovedMemories:
    """The participant: every memory that depended on a deleted record.

    It works from the **reference**, never from the row, which is what lets it run
    after the owner has already removed the target: a link naming a memory outlives
    that memory, because a reference is a value and carries no foreign key.

    A reference of some other type answers "removed nothing" rather than raising: a
    participant that raised would abort a deletion it has no stake in, and the
    coordinator only offers it references it registered for anyway.
    """
    if ref.module != MODULE_ID or ref.record_type != MEMORY_RECORD_TYPE:
        return NOTHING_REMOVED
    return RemovedMemories(
        _erase(
            uow.connection,
            dependent_closure(uow.connection, ref.id, include_marked=True),
        )
    )
