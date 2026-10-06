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

**The lock is not the only guard, and that is deliberate** (issue #12). Every write here
that advances a revision is itself a compare-and-set, ``... WHERE id = $id AND
revision = $expected``, through the repository, and a write that matches no row raises
:class:`~rheo_contracts.StaleRecord`, which the dispatcher answers ``record_stale``. The
lock serializes every writer that takes it, so between two of those the predicate never
fires; it is there for the writer that forgot the lock, which would otherwise lose an
update without any signal.

**What each disposition does** (§ A5's closure table, and this file implements it
verbatim):

- *Correct* rewrites the target's title, body and confidence in place, bumps its
  revision, stamps ``corrected_at``, drops its embeddings (and queues a fresh embed
  job when the workspace can use one), and marks the affected closure
  ``source_corrected``.
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
- *Scheduled expiry* is the same two halves over a **narrower** closure: ordinary
  dependents only, every marked lineage hop skipped, so a fresh replacement survives
  its expired predecessor's sweep. The coordinator says which of the two is running
  through :class:`~rheo_core.deletion.registry.Disposition`, because neither
  :func:`authorize_memory_delete` nor :func:`on_record_deleted` can see the sealed
  authority core verified on its way here — and both of them answer differently
  depending on it.

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
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from rheo_contracts import RecordRef, StaleRecord, WorkspaceContext
from rheo_core.boundary.context import Refusal
from rheo_core.deletion import (
    NOTHING_REMOVED,
    DeleteAuthorization,
    Disposition,
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
    expiry_admits,
    memory_reference,
)
from rheo_recallatron.embedding.enqueue import enqueue_embed_job
from rheo_recallatron.entities import remove_mention
from rheo_recallatron.events import (
    MEMORY_INVALIDATED,
    consumers_for_dispatch,
    publish_memory_event,
)
from rheo_recallatron.references import canonical_ref
from rheo_recallatron.refusals import (
    NOT_FOUND,
    RECORD_STALE,
    REFERENCE_SCAN_LIMIT,
    RETENTION_UNAVAILABLE,
)
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

DELETION_PARTICIPANT_TYPES: Final = (
    f"{MODULE_ID}.{MEMORY_RECORD_TYPE}",
    "leads.observation",
    "leads.opportunity",
    "relationships.party",
)
"""Reference-only subscriptions; no import or storage access to those modules.

Confirmed user erasure removes directly linked memories and their derived closure.
Scheduled memory expiry retains its existing, narrower supersession-lineage rule.
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
    return _reference_closure(
        conn,
        RecordRef.parse(memory_reference(target_id)),
        include_marked=include_marked,
    )


def _reference_closure(
    conn: Connection, target: RecordRef, *, include_marked: bool
) -> tuple[UUID, ...]:
    is_memory = target.module == MODULE_ID and target.record_type == MEMORY_RECORD_TYPE
    seen: set[UUID] = {target.id} if is_memory else set()
    found: list[UUID] = []
    frontier: deque[tuple[str, bool]] = deque([(target.format(), True)])
    while frontier:
        current, first_hop = frontier.popleft()
        for link in list_memory_dependents(conn, current):
            if link.supersession_lineage and not include_marked:
                continue
            if not first_hop and link.relation != RELATION_DERIVED_FROM:
                continue
            if link.memory_id in seen:
                continue
            seen.add(link.memory_id)
            found.append(link.memory_id)
            frontier.append((memory_reference(link.memory_id), False))
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
) -> int | None:
    """Mark one closure row non-erasure invalidated and publish it; ``None`` if it
    already was.

    A closure row is compared against the revision read here, because nothing earlier
    in the operation read it: the closure is a set of identifiers. A writer that
    skipped the lifecycle lock and moved the row after this read makes the write raise
    ``StaleRecord``, and the whole operation rolls back.
    """
    row = get_memory(uow.connection, memory_id)
    if row is None or row.invalidation_reason is not None:
        return None
    return _invalidate_row(
        ctx,
        uow,
        row,
        expected_revision=row.revision,
        reason=reason,
        now=now,
        consumers=consumers,
    )


def _retire_predecessor(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    predecessor: MemoryRow,
    *,
    successor: UUID,
    now: datetime,
    consumers: ConsumerRegistry,
) -> int:
    """Supersession's step 4: invalidate the predecessor **at the revision** ``_target``
    **compared**, never at one re-read here.

    Everything the supersession built (the replacement's purposes, links and the
    closure it invalidated) was derived from the row ``_target`` read. Re-reading the
    revision at this point would let a lock-skipping writer that committed in between
    move the row to *n+1*, and this write would then retire content the caller never
    saw at *n+2* and report success. Compared against ``predecessor.revision``, that
    writer makes this raise ``StaleRecord`` instead (issue #12).

    A predecessor that is gone or already retired by then is the same case, a copy
    that moved, so it is ``StaleRecord`` too, not a silent ``None`` and not an
    assertion that would surface as ``handler_failed``.
    """
    row = get_memory(uow.connection, predecessor.id)
    if (
        row is None
        or row.invalidation_reason is not None
        or row.superseded_by_id is not None
    ):
        raise StaleRecord("that memory has moved since the revision you hold")
    return _invalidate_row(
        ctx,
        uow,
        row,
        expected_revision=predecessor.revision,
        reason=SOURCE_SUPERSEDED,
        now=now,
        consumers=consumers,
        successor=successor,
    )


def _invalidate_row(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    row: MemoryRow,
    *,
    expected_revision: int,
    reason: str,
    now: datetime,
    consumers: ConsumerRegistry,
    successor: UUID | None = None,
) -> int:
    """The compare-and-set mark, the embedding delete and the publish, for one row.

    The mark and the embedding delete are one transaction, so no committed state has a
    vector outliving the row's claim to be current. **The mark goes first**, and that
    order is the vector-write handshake's half on this side (see the repository): the
    ``UPDATE`` takes the row lock an in-flight embed holds ``FOR SHARE``, so it waits
    for that embed to commit, and the delete that follows removes the vector it wrote.
    Deleting first would find nothing, and the embed's vector would then commit onto a
    row that is no longer current.
    """
    revision = invalidate_memory(
        uow.connection,
        row.id,
        reason=reason,
        invalidated_at=now,
        expected_revision=expected_revision,
        superseded_by_id=successor,
    )
    delete_memory_embeddings(uow.connection, row.id)
    publish_memory_event(
        ctx,
        uow,
        event_type=MEMORY_INVALIDATED,
        memory_ref=memory_reference(row.id),
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
    # **Rewrite first, then delete the old text's vectors — the order is the fix.** The
    # rewrite takes the row lock an in-flight embed holds ``FOR SHARE`` from reading
    # the old text until its vector commits, so it waits for that embed, and the delete
    # below (a fresh statement, after the wait) then removes the vector it committed.
    # An embed that arrives after the rewrite waits for this transaction and reads the
    # new text. Deleting first would find nothing to delete while an embed of the old
    # text was still to commit, and that stale vector would stay for good: every later
    # fill skips a memory that already has one.
    #
    # The rewrite is also the compare-and-set: ``AND revision = $expected``, with the
    # revision ``_target`` already compared under the lock. The lock is what serializes
    # writers that take it; the predicate is what still refuses one that did not.
    revision = correct_memory(
        uow.connection,
        row.id,
        title=model_input.title,
        body=model_input.body,
        confidence=model_input.confidence,
        expected_revision=row.revision,
        corrected_at=request.now,
    )
    delete_memory_embeddings(uow.connection, row.id)
    # The new text gets a vector on the same terms a new memory does. ``write_memory``'s
    # own call never runs here, because a correction rewrites the row in place.
    enqueue_embed_job(ctx, uow, memory_id=row.id, now=request.now)
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
    # (4) the predecessor last, so its successor pointer names a row that exists, and
    # at the revision (1) compared: a writer that skipped the lock and moved it since
    # makes this ``StaleRecord``, which rolls the replacement back with everything else.
    revision = _retire_predecessor(
        ctx,
        uow,
        predecessor,
        successor=canonical_ref(written.ref).id,
        now=request.now,
        consumers=consumers,
    )
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
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    ref: RecordRef,
    *,
    disposition: Disposition,
) -> DeleteAuthorization | Refusal:
    """The owned-delete authorizer: a reference and a revision, or a refusal.

    Content-free by shape and by content: it answers
    :class:`~rheo_core.deletion.DeleteAuthorization`, which has nowhere to put a
    title or a body, and it reads nothing beyond what
    :func:`~rheo_recallatron.eligibility.deletion_admits` or
    :func:`~rheo_recallatron.eligibility.expiry_admits` needs.

    **Two dispositions, two questions, and they are not narrowings of each other.**

    *User erasure* asks whose record this is: the module enabled here, one of § A10's
    two lifecycle roles, the row's audience, and the caller's bound purpose. It
    permits an authorized **retained or expired** row, which is § A7's wording: the
    approved path does not reach one anyway, because the core's record-state guard
    resolves the subject in ``current`` mode before the effect.

    *Scheduled expiry* asks nothing about the caller at all and asks instead whether
    the row is genuinely past the workspace's current retention horizon, re-read here
    under the lifecycle lock. A sweep is accountless and carries neither lifecycle
    role, so running it through the erasure question would refuse **every** memory in
    the workspace and expire nothing at all; running it through the erasure question
    with the roles widened would let a service token erase what a person may not. The
    authority is instead the sealed scheduled execution the core verified before this
    module was reached, which is exactly what ``disposition`` reports.

    A missing retention policy is the sweep's ``retention_unavailable`` rather than a
    ``not_found``: the record may well be there, and the thing that is missing is the
    workspace's own statement of how long it keeps it.
    """
    if ref.module != MODULE_ID or ref.record_type != MEMORY_RECORD_TYPE:
        return _unauthorized(ref)
    row = get_memory(uow.connection, ref.id)
    if row is None:
        return _unauthorized(ref)
    if disposition is Disposition.SCHEDULED_EXPIRY:
        expired = expiry_admits(ctx, uow, row)
        if expired is None:
            return Refusal(
                RETENTION_UNAVAILABLE,
                "this workspace states no usable retention policy",
            )
        if not expired:
            return _unauthorized(ref)
        return DeleteAuthorization(ref=ref, revision=row.revision)
    if not deletion_admits(ctx, uow, row):
        return _unauthorized(ref)
    return DeleteAuthorization(ref=ref, revision=row.revision)


def _erase(
    conn: Connection,
    memory_ids: Iterable[UUID],
    *,
    expected_revisions: Mapping[UUID, int] | None = None,
) -> frozenset[UUID]:
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

    ``expected_revisions`` binds a row's delete to the revision an authorization
    checked (issue #275): the owner passes its target's, and the closure passes none.
    A bound row that is gone, or no longer at that revision, raises
    :class:`~rheo_contracts.StaleRecord` and the whole deletion rolls back, including
    the mentions and receipt this loop has already touched.
    """
    bound = {} if expected_revisions is None else expected_revisions
    removed: set[UUID] = set()
    for memory_id in sorted(memory_ids):
        expected = bound.get(memory_id)
        row = get_memory(conn, memory_id)
        if row is None:
            if expected is not None:
                raise StaleRecord("that memory has moved since the revision you hold")
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
        if delete_memory(conn, memory_id, expected_revision=expected):
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

    **It takes no ``disposition`` and needs none**, unlike the authorizer and the
    participant either side of it. It traverses nothing — the one row the
    authorization names, and the children that hang off it by cascade — so there are
    no edges for a disposition to choose between, and "physically remove the target"
    is the same instruction whether a person asked or a timer did.

    **The delete is bound to the authorized revision** (issue #275), the same rule
    #12 gave correct and supersede: the lock orders the writers that take it, and
    ``AND revision = $authorized`` is what refuses when one that skipped it moved the
    row between the third authorization and this statement. A miss raises
    ``StaleRecord``. On the approved path the dispatcher answers ``record_stale`` and
    rolls back, leaving the approval pending; approving it again is refused by the
    record-state guard, because the row no longer holds the approved revision. On the
    expiry path the sweep job fails its attempt,
    rolls back whole, and is retried, so the next pass re-asks whether the moved row
    is still expired.
    """
    target = authorization.ref.id
    return RemovedMemories(
        _erase(
            uow.connection,
            (target,),
            expected_revisions={target: authorization.revision},
        )
    )


def on_record_deleted(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    ref: RecordRef,
    *,
    disposition: Disposition,
) -> RemovedMemories:
    """The participant: every memory that depended on a deleted record.

    It works from the **reference**, never from the row, which is what lets it run
    after the owner has already removed the target: a link naming a memory outlives
    that memory, because a reference is a value and carries no foreign key.

    A reference of some other type answers "removed nothing" rather than raising: a
    participant that raised would abort a deletion it has no stake in, and the
    coordinator only offers it references it registered for anyway.

    **``disposition`` chooses the edges, and it is the one line in this module where
    getting it wrong destroys something nobody asked to lose.** § A5's closure table
    gives user erasure the marked supersession-lineage hops — "copied ancestry keeps
    closure connected across missing middle rows", so erasing A still reaches the C
    that replaced the B that replaced it — and gives scheduled expiry the ordinary
    edges only, so *a replacement reached only by ancestry survives on its own clock*.
    Following ancestry on the expiry path would mean that sweeping an expired
    predecessor silently took the fresh replacement written yesterday with it, and no
    assertion that watched only the predecessor would ever notice.

    A replacement reached by an **ordinary** path is still removed on both, which is
    the other half of the same table row and the reason this is a choice of edges
    rather than a choice of whether to cascade at all.
    """
    if f"{ref.module}.{ref.record_type}" not in DELETION_PARTICIPANT_TYPES:
        return NOTHING_REMOVED
    is_memory = ref.module == MODULE_ID and ref.record_type == MEMORY_RECORD_TYPE
    if not is_memory and disposition is not Disposition.USER_ERASURE:
        return NOTHING_REMOVED
    return RemovedMemories(
        _erase(
            uow.connection,
            _reference_closure(
                uow.connection,
                ref,
                include_marked=disposition is not Disposition.SCHEDULED_EXPIRY,
            ),
        )
    )
