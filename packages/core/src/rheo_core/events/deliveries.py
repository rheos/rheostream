"""Every read and write against ``core.event_delivery`` and
``core.consumer_processed``: the ordered lease, the three finishes, the requeue, the
dedup ledger, the three writes a person makes (retry, skip, replay), and the reads a
deliverer, the failure list and the failure summary need.

Shaped after ``rheo_core.work.jobs`` function for function. Each function takes the
caller's ``Connection`` and runs inside the caller's transaction, and **none of them
commits**. ``now`` is always an explicit keyword parameter: nothing here reads the
process clock, so a test drives lease expiry by choosing an instant rather than by
sleeping (``jobs.py:6-8``).

**Two deviations from ``docs/architecture/intake-and-events.md`` § Ordering**,
recorded here so a reviewer finds them without re-deriving them:

1. **``:now`` is a bound parameter**, never SQL ``now()``, and ``lease_seconds`` is
   the caller's rather than the document's literal ``60``. One parameter drives every
   predicate of the one statement it appears in, so the lease query's two ``WHERE``
   arms can never disagree; server-side ``now()`` would also be uncontrollable from a
   test and would force real sleeps to prove expiry. ``work/jobs.py`` deviates
   identically and for the same reason, and the multi-host clock-skew assumption
   recorded in its docstring carries here unchanged.
2. **``attempts`` is incremented by the lease**, which the ratified statement does not
   show. The retry schedule is indexed by the attempt being made, so the count has to
   advance where the attempt starts; incrementing on a failure path instead would
   leave a delivery that crashed its worker with a count that never moved, and the
   expired-lease arm **is** the crash-recovery path.
**Why the three writes that end an attempt carry the ownership half *and* the dedup
ledger.** Each is predicated on ``event_id``, ``consumer_id``, ``lease_owner = :owner``
and ``state = 'leased'`` (:func:`_held`), exactly as ``work/jobs.py`` does, and returns
whether it applied. The ledger is not a substitute for that predicate, because the two
guard different things: ``consumer_processed``'s primary key stops a *handler* running
twice, while the predicate stops a worker whose lease lapsed mid-handler from writing
the *outcome* of a row its new owner now holds.

Only the requeue branch of that race would be self-healing without the predicate — a
row flipped back to ``pending`` is re-leased, the ledger is found, the handler is not
re-run, and the extra attempt is absorbed. **The fail branch is not.** ``failed`` is not
in :func:`lease_delivery`'s candidate set, and the only writes that move a row out of
it are a person's (:func:`retry_delivery`, :func:`skip_delivery` and
:func:`replay_deliveries`, reached through ``core.work.retry``, ``.skip`` and
``.replay``), so a lapsed owner's :func:`fail_delivery` would strand its whole
``(consumer_id, subject_ref)`` queue behind a head that was in fact processed exactly
once, until someone noticed and acted.

Every write is predicated and returns whether it applied, exactly as ``jobs.py:40-46``
requires: an unpredicated ``UPDATE`` here would move a delivery out from under the
worker that is running it.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import (
    Connection,
    and_,
    delete,
    func,
    insert,
    literal,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql.elements import ColumnElement

from rheo_core.storage import work_tables as t
from rheo_core.work.backoff import backoff_for

PENDING: Final = "pending"
LEASED: Final = "leased"
DELIVERED: Final = "delivered"
FAILED: Final = "failed"
SKIPPED: Final = "skipped"
"""The five members of the ``event_delivery_state`` check constraint
(``work_tables.DELIVERY_STATES``), named here so no predicate in this module spells
one of them by hand."""

_SETTLED: Final = (DELIVERED, SKIPPED)
"""The two states that release the head of a ``(consumer_id, subject_ref)`` queue.
A ``failed`` head deliberately does not: order is never broken silently."""


@dataclass(frozen=True, slots=True)
class LeasedDelivery:
    """The row :func:`lease_delivery` just took, as a deliverer needs to see it.

    ``attempts`` is the value **after** this lease counted its attempt, so it is the
    attempt number the envelope carries and the index the retry schedule reads.

    **No ``owner`` field**, though every write that ends this attempt needs one:
    ``lease_delivery``'s caller supplied it and still holds it, so handing it back
    would be a second copy of the caller's own value — one that a later edit can let
    drift from the one actually passed to the finish. ``work.jobs.LeasedJob`` omits it
    for the same reason, and ``jobs.py``'s four finishes take it from their caller.
    """

    event_id: UUID
    consumer_id: str
    subject_ref: str
    position: int
    attempts: int


@dataclass(frozen=True, slots=True)
class FailedDeliveryRow:
    """One row of the delivery failure list, as the database holds it.

    Six fields: the delivery's identity, its attempts, its error, its completion
    instant, and how many later deliveries of the same consumer and subject it holds
    back. Deliberately **not** a field-for-field copy of
    ``work.jobs.FailedJobRow`` — ``event_delivery`` has no ``max_attempts`` column,
    because a delivery's budget is the ``work.max_attempts`` setting rather than a
    per-row value.
    """

    event_id: UUID
    consumer_id: str
    attempts: int
    last_error: str | None
    completed_at: datetime | None
    blocked_count: int


def _row(event_id: UUID, consumer_id: str) -> ColumnElement[bool]:
    """The composite primary key, as a predicate."""
    return and_(
        t.event_delivery.c.event_id == event_id,
        t.event_delivery.c.consumer_id == consumer_id,
    )


def _held(event_id: UUID, consumer_id: str, owner: str) -> ColumnElement[bool]:
    """The predicate every write that ends an attempt shares, named after
    ``jobs.py:_held`` because it is the same guard over the same hazard.

    One helper rather than three hand-written copies: three is the shape a later edit
    updates in two places.
    """
    return and_(
        _row(event_id, consumer_id),
        t.event_delivery.c.lease_owner == owner,
        t.event_delivery.c.state == LEASED,
    )


def _apply(conn: Connection, where: ColumnElement[bool], **values: object) -> bool:
    """Run one predicated ``UPDATE`` on ``core.event_delivery`` and report whether it
    matched."""
    return (
        conn.execute(update(t.event_delivery).where(where).values(**values)).rowcount
        == 1
    )


def lease_delivery(
    conn: Connection, *, owner: str, now: datetime, lease_seconds: int
) -> LeasedDelivery | None:
    """Lease at most one due delivery for ``owner``, or ``None`` when none is due.

    The ratified ordered lease (``intake-and-events.md:312-330``). Three clauses, each
    load-bearing:

    * ``FOR UPDATE SKIP LOCKED`` on the inner select is what makes two workers take
      two different rows instead of one blocking on the other.
    * The second ``WHERE`` arm — a ``leased`` row whose ``lease_until`` has passed —
      **is** the crash-recovery path, and re-acquiring counts the attempt.
    * The correlated ``NOT EXISTS`` is the whole of the ordering rule: a delivery is
      not a candidate while any earlier delivery for the same
      ``(consumer_id, subject_ref)`` is in a state other than ``delivered`` or
      ``skipped``. A ``failed`` head therefore blocks its own queue until a person
      acts, which is deliberate — order is never broken silently. Across subjects
      there is no ordering promise, and ``ORDER BY position`` orders only the
      candidates that survive the clause.
    """
    candidate = t.event_delivery.alias("candidate")
    earlier = t.event_delivery.alias("earlier")
    blocked = (
        select(literal(1))
        .where(
            earlier.c.consumer_id == candidate.c.consumer_id,
            earlier.c.subject_ref == candidate.c.subject_ref,
            earlier.c.position < candidate.c.position,
            earlier.c.state.not_in(_SETTLED),
        )
        .exists()
    )
    due = (
        select(candidate.c.event_id, candidate.c.consumer_id)
        .where(
            or_(
                and_(
                    candidate.c.state == PENDING,
                    candidate.c.next_attempt_at <= now,
                ),
                and_(candidate.c.state == LEASED, candidate.c.lease_until < now),
            )
        )
        .where(~blocked)
        .order_by(candidate.c.position)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    row = conn.execute(
        update(t.event_delivery)
        .where(
            tuple_(t.event_delivery.c.event_id, t.event_delivery.c.consumer_id).in_(due)
        )
        .values(
            state=LEASED,
            lease_owner=owner,
            lease_until=now + timedelta(seconds=lease_seconds),
            attempts=t.event_delivery.c.attempts + 1,
        )
        .returning(
            t.event_delivery.c.event_id,
            t.event_delivery.c.consumer_id,
            t.event_delivery.c.subject_ref,
            t.event_delivery.c.position,
            t.event_delivery.c.attempts,
        )
    ).one_or_none()
    if row is None:
        return None
    return LeasedDelivery(
        event_id=row.event_id,
        consumer_id=str(row.consumer_id),
        subject_ref=str(row.subject_ref),
        position=int(row.position),
        attempts=int(row.attempts),
    )


def mark_delivered(
    conn: Connection, *, event_id: UUID, consumer_id: str, owner: str, now: datetime
) -> bool:
    """End the delivery ``delivered``. Returns whether the write applied.

    ``last_error`` is cleared: a success erases the error an earlier attempt left
    behind, exactly as ``jobs.finish_succeeded`` does.

    **What a ``False`` means, once, for all three of the writes that carry
    :func:`_held`.** The lease is no longer this owner's ``leased`` row — it lapsed and
    someone else took it. The caller must roll its own transaction back rather than
    commit the handler's effects against a row it no longer owns; committing first and
    checking the return value afterwards ships duplicated effects on every lease theft.
    """
    return _apply(
        conn,
        _held(event_id, consumer_id, owner),
        state=DELIVERED,
        completed_at=now,
        lease_owner=None,
        lease_until=None,
        last_error=None,
    )


def requeue_delivery(
    conn: Connection,
    *,
    event_id: UUID,
    consumer_id: str,
    owner: str,
    now: datetime,
    error: str,
    jitter: random.Random | None,
) -> bool:
    """Put the delivery back ``pending`` under its backoff. Returns whether it applied.

    The delay comes from ``work.backoff.backoff_for`` — **one retry policy for jobs
    and deliveries alike** (D6), so this is a caller of that schedule and never a
    second copy of it. ``attempts`` is read from the row rather than taken as a
    parameter, because the lease already counted it and a caller passing its own
    count is a caller that can disagree with the row. The read is unpredicated and the
    write is not: reading a count this owner may no longer be entitled to act on costs
    nothing, because the write that uses it still has to match :func:`_held`.

    ``jitter`` is a required keyword with no default: a test passes ``None`` and gets
    the bare schedule, and production passes a source. Nothing here decides whether
    the budget is spent — that is the deliverer's, which holds the setting.
    """
    attempts = conn.execute(
        select(t.event_delivery.c.attempts).where(_row(event_id, consumer_id))
    ).scalar_one_or_none()
    if attempts is None:
        return False
    return _apply(
        conn,
        _held(event_id, consumer_id, owner),
        state=PENDING,
        next_attempt_at=now + backoff_for(int(attempts), jitter=jitter),
        lease_owner=None,
        lease_until=None,
        last_error=error,
    )


def fail_delivery(
    conn: Connection,
    *,
    event_id: UUID,
    consumer_id: str,
    owner: str,
    now: datetime,
    error: str,
) -> bool:
    """End the delivery ``failed`` with ``error``. Returns whether the write applied.

    The row is written, never deleted: a failed delivery is a blocked head its queue
    stays behind until a person acts, and a deleted row would release the queue
    silently.

    **The write :func:`_held` matters most for.** ``failed`` is the one state no worker
    moves a row out of, and a failed head blocks every later delivery for its consumer
    and subject until a person retries or skips it. A worker whose lease lapsed
    mid-handler writing this against the row its successor now holds would strand that
    queue behind an event that was in fact processed exactly once.
    """
    return _apply(
        conn,
        _held(event_id, consumer_id, owner),
        state=FAILED,
        completed_at=now,
        lease_owner=None,
        lease_until=None,
        last_error=error,
    )


def retry_delivery(
    conn: Connection, *, event_id: UUID, consumer_id: str, now: datetime
) -> bool:
    """Put a ``failed`` delivery back ``pending`` with a fresh budget. Returns whether
    it applied.

    ``core.work.retry``'s write. ``attempts`` goes back to zero, which is what "resets
    attempts" means (``intake-and-events.md`` § Retry): the next lease counts attempt
    one, the backoff schedule starts again at its first step, and the pre-handler
    budget gate in ``work/loop.py`` grants the whole ``work.max_attempts`` again.
    ``next_attempt_at`` is ``now``, so the row is a lease candidate on the next visit
    rather than after a backoff a person has already waited out.

    **``last_error`` is kept.** It is the only evidence of why the row failed, and the
    first attempt that succeeds clears it (:func:`mark_delivered`); a retry that fails
    again overwrites it. ``completed_at`` is cleared because the row is no longer
    terminal.

    **Predicated on ``failed``**, for :func:`skip_delivery`'s reason: a ``pending`` or
    ``leased`` row is already on its way, and resetting a ``leased`` row's attempts
    would feed the worker running it a count that no longer matches its lease. A
    ``delivered`` or ``skipped`` row is settled, and re-running one is replay's job,
    which has its own gate.
    """
    return _apply(
        conn,
        and_(_row(event_id, consumer_id), t.event_delivery.c.state == FAILED),
        state=PENDING,
        attempts=0,
        next_attempt_at=now,
        lease_owner=None,
        lease_until=None,
        completed_at=None,
    )


SKIP_NOTE_PREFIX: Final = "skipped: "
"""What a skipped row's ``last_error`` starts with; the person's note follows."""

SKIP_PREVIOUS_ERROR: Final = "\nlast error: "
"""The separator before the error the row failed with, when it had one."""


def skip_delivery(
    conn: Connection,
    *,
    event_id: UUID,
    consumer_id: str,
    now: datetime,
    note: str | None = None,
) -> bool:
    """Release a ``failed`` head, setting it ``skipped``. Returns whether it applied.

    **Predicated on ``failed``, not unconditional.** Releasing a head that a person
    has looked at and given up on is this write's whole purpose; an unpredicated
    ``UPDATE ... SET state = 'skipped'`` would just as happily skip a ``pending`` or
    ``leased`` delivery out from under a worker that is, or is about to be, actually
    processing it — discarding work in flight with nothing to show for it. Every
    write in ``work/jobs.py`` carries a predicate for the same reason.

    **It takes no ``owner``, and the asymmetry with the three writes above is
    deliberate.** Those end an attempt a worker is in the middle of, so they must prove
    the lease is still that worker's. This one releases a ``failed`` row, whose
    ``lease_owner`` :func:`fail_delivery` already cleared, on behalf of a person rather
    than a worker — there is no lease to hold, and the ``failed`` predicate is what
    stands in its place.

    **The note lands in ``last_error``, a recorded deviation of the same shape as
    ``operations/records.py``'s ``resolve``.** ``event_delivery`` has no ``note``
    column, and a migration for one is more than this write needs. So the column
    becomes ``skipped: <note>`` followed by the error the row failed with, which is
    kept rather than overwritten: a skipped row answers both "why did it fail" and "why
    was it let go". **The row is the note's only readable home.** ``core.work.skip``'s
    audit row keeps a digest of the request, which can confirm a note someone quotes
    but cannot recover one, which is why :func:`replay_deliveries` keeps
    ``last_error`` on the rows it resets. ``note`` is optional here only so the
    repository tests can skip without one; the operation always passes it.
    """
    values: dict[str, object] = {
        "state": SKIPPED,
        "completed_at": now,
        "lease_owner": None,
        "lease_until": None,
    }
    if note is not None:
        values["last_error"] = func.concat(
            literal(SKIP_NOTE_PREFIX + note),
            func.coalesce(
                literal(SKIP_PREVIOUS_ERROR) + t.event_delivery.c.last_error,
                literal(""),
            ),
        )
    return _apply(
        conn,
        and_(_row(event_id, consumer_id), t.event_delivery.c.state == FAILED),
        **values,
    )


def delivery_state(conn: Connection, *, event_id: UUID, consumer_id: str) -> str | None:
    """The delivery's state, or ``None`` when there is no such row.

    What lets ``core.work.retry`` and ``.skip`` tell "no such delivery" from "not
    failed" in their refusals. The predicated write is still what makes the refusal
    true; this read only names it.
    """
    value = conn.execute(
        select(t.event_delivery.c.state).where(_row(event_id, consumer_id))
    ).scalar_one_or_none()
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class ReplayCounts:
    """What :func:`replay_deliveries` did: rows put back, and rows made."""

    reset: int
    created: int


def oldest_retained_position(conn: Connection) -> int | None:
    """The smallest ``position`` still in ``core.outbox_event``, or ``None`` when the
    outbox is empty.

    Replay's horizon. Taken over every event type, because retention removes rows by
    age and not by type, so the oldest row of any type is the point before which the
    outbox can no longer be trusted to be whole.
    """
    value = conn.execute(select(func.min(t.outbox_event.c.position))).scalar_one()
    return None if value is None else int(value)


REPLAY_LOCK: Final = "LOCK TABLE core.event_delivery IN SHARE ROW EXCLUSIVE MODE"
"""The table lock a replay takes before it reads or writes a delivery row."""


def lock_for_replay(
    conn: Connection, *, consumer_id: str, from_position: int
) -> UUID | None:
    """Lock ``core.event_delivery`` for the rest of the caller's transaction, then name
    a delivery of ``consumer_id`` at or after ``from_position`` that is ``leased``, or
    ``None`` when none is.

    **A table lock, because a row lock covers only rows that already exist.** With row
    locks alone, a publish that commits while the replay is open fans out a *later*
    event for a subject whose earlier row the replay has reset but not committed; a
    worker still sees that earlier row ``delivered``, leases the later one, and after
    the replay commits a second worker leases the earlier one. Two deliveries of one
    subject are then in flight and the older event is applied after the newer, which
    is the one thing the ordering rule forbids.

    ``SHARE ROW EXCLUSIVE`` conflicts with ``ROW EXCLUSIVE``, the lock every
    ``INSERT``, ``UPDATE`` and ``DELETE`` takes, and with itself, and with nothing a
    plain read takes. So until the replay commits: no fan-out insert, no lease, no
    requeue and no finish lands anywhere in the table, a second replay waits, and
    readers (the failure list, the summary) carry on. A blocked worker's statement
    takes its snapshot after the lock is granted, so it sees the replay's rows. It
    must be the transaction's first touch of the table, or a writer already holding
    a row lock could deadlock against it; ``core.work.replay`` takes it first.

    The cost is a workspace-wide pause of deliveries for one replay transaction, which
    is an operator's act bounded by the retained outbox.

    Once the lock is held nothing can lease a row, so the in-flight check below stays
    true until the commit.
    """
    conn.exec_driver_sql(REPLAY_LOCK)
    found = conn.execute(
        select(t.event_delivery.c.event_id)
        .where(
            t.event_delivery.c.consumer_id == consumer_id,
            t.event_delivery.c.position >= from_position,
            t.event_delivery.c.state == LEASED,
        )
        .limit(1)
    ).scalar_one_or_none()
    return None if found is None else UUID(str(found))


def replay_deliveries(
    conn: Connection,
    *,
    consumer_id: str,
    event_type: str,
    from_position: int,
    now: datetime,
) -> ReplayCounts:
    """Re-create ``pending`` deliveries for one consumer from the outbox, from
    ``from_position`` on. Does not commit.

    ``core.work.replay``'s write; the gates (``replay_safe``, the enabled module, the
    retention horizon, no lease in flight) are the operation's, and this function
    assumes them and assumes the caller holds :func:`lock_for_replay`'s table lock.

    **Existing rows are reset, never duplicated.** The primary key is
    ``(event_id, consumer_id)``, so one event has at most one delivery per consumer
    and a second row is not a thing that can exist. A ``delivered``, ``failed`` or
    ``skipped`` row for a retained event of ``event_type`` at or after the position is
    put back ``pending`` with ``attempts = 0`` and ``next_attempt_at = now``. A
    ``pending`` row is left alone, because it is already going to be delivered, and a
    ``leased`` one is the caller's refusal. A retained event with no row for this
    consumer at all (it was published before the consumer's module was enabled, or
    before the consumer existed) gets a new one, which is how a new read model is
    built from history. ``ON CONFLICT DO NOTHING`` keeps a publish that fans out to
    the same consumer concurrently from turning into a conflict.

    **``last_error`` is kept on every reset row.** For a formerly ``skipped`` row it is
    the person's note, and the row is its only readable home (the audit row keeps a
    digest, which can confirm a note but not recover one); the first attempt that
    succeeds clears it, as :func:`mark_delivered` always does.

    **The dedup ledger rows for every in-range event of ``event_type`` are deleted**,
    for this consumer only and in the same transaction: not only for the rows reset or
    created, but for ``pending`` ones too, which a requeue after a lost finish can
    leave with a ledger row. Without that the worker finds ``consumer_processed`` for
    such a row, marks it ``delivered`` without running the handler, and the rebuild
    silently skips it. Deleting them is what "replay" means for a
    consumer that declared itself ``replay_safe``: its handler is run again, on
    purpose, and it has promised that doing so only rebuilds derived state.

    **Order is the lease query's, unchanged.** Each row keeps the event's own
    ``position`` and ``subject_ref``, so within one subject the reset rows are leased
    oldest first and a later row waits behind an earlier one exactly as it did the
    first time.
    """
    settled_or_failed = (DELIVERED, FAILED, SKIPPED)
    replayable = select(t.outbox_event.c.id).where(
        t.outbox_event.c.type == event_type,
        t.outbox_event.c.position >= from_position,
    )
    reset_ids = [
        row.event_id
        for row in conn.execute(
            update(t.event_delivery)
            .where(
                t.event_delivery.c.consumer_id == consumer_id,
                t.event_delivery.c.position >= from_position,
                t.event_delivery.c.state.in_(settled_or_failed),
                t.event_delivery.c.event_id.in_(replayable),
            )
            .values(
                state=PENDING,
                attempts=0,
                next_attempt_at=now,
                lease_owner=None,
                lease_until=None,
                completed_at=None,
            )
            .returning(t.event_delivery.c.event_id)
        )
    ]
    missing = (
        select(
            t.outbox_event.c.id,
            literal(consumer_id),
            t.outbox_event.c.subject_ref,
            t.outbox_event.c.position,
            literal(PENDING),
            literal(0),
            literal(now),
        )
        .where(
            t.outbox_event.c.type == event_type,
            t.outbox_event.c.position >= from_position,
            ~select(literal(1))
            .where(
                t.event_delivery.c.event_id == t.outbox_event.c.id,
                t.event_delivery.c.consumer_id == consumer_id,
            )
            .exists(),
        )
        .order_by(t.outbox_event.c.position)
    )
    created_ids = [
        row.event_id
        for row in conn.execute(
            pg_insert(t.event_delivery)
            .from_select(
                [
                    "event_id",
                    "consumer_id",
                    "subject_ref",
                    "position",
                    "state",
                    "attempts",
                    "next_attempt_at",
                ],
                missing,
            )
            .on_conflict_do_nothing(index_elements=["event_id", "consumer_id"])
            .returning(t.event_delivery.c.event_id)
        )
    ]
    conn.execute(
        delete(t.consumer_processed).where(
            t.consumer_processed.c.consumer_id == consumer_id,
            t.consumer_processed.c.event_id.in_(replayable),
        )
    )
    return ReplayCounts(reset=len(reset_ids), created=len(created_ids))


def record_processed(
    conn: Connection, *, consumer_id: str, event_id: UUID, now: datetime
) -> None:
    """Write the dedup ledger row for ``(consumer_id, event_id)``. Does not commit.

    **A plain ``INSERT``, never ``ON CONFLICT DO NOTHING``, and the conflict is
    allowed to raise.** ``consumer_processed``'s composite primary key exists so that
    a second insert for an already-processed pair is a *refusal* rather than a repeat,
    and that refusal is the backstop under exactly-once delivery — independent of
    whatever check a caller makes first, and the one guard that still holds when a
    lapsed lease leaves two workers inside the same handler at once. :func:`_held`
    guards the *row's outcome* against that same race; this guards the *handler*, and
    neither substitutes for the other. Swallowing the conflict here would
    turn the backstop into a no-op that no correct-path test can see, so the
    ``IntegrityError`` propagates to the caller, whose transaction it must roll back.
    """
    conn.execute(
        insert(t.consumer_processed).values(
            consumer_id=consumer_id, event_id=event_id, processed_at=now
        )
    )


def already_processed(conn: Connection, *, consumer_id: str, event_id: UUID) -> bool:
    """Whether this consumer has already processed this event.

    The cheap check a deliverer makes before running a handler. It is **not** the
    guarantee — two callers can both read ``False`` — which is why
    :func:`record_processed` is allowed to raise.
    """
    return (
        conn.execute(
            select(literal(1)).where(
                t.consumer_processed.c.consumer_id == consumer_id,
                t.consumer_processed.c.event_id == event_id,
            )
        ).first()
        is not None
    )


def _blocked_behind(head: Any) -> ColumnElement[bool]:
    """Whether a delivery row is held back by ``head``, a ``failed`` row of the same
    consumer and subject that comes before it.

    Everything later in the queue that is not itself settled or failed: a row behind
    a failed head is never leased, so it can only be ``pending`` (or ``leased`` from a
    lease taken before the head failed, which the count still owes a person).
    """
    behind = t.event_delivery
    return and_(
        behind.c.consumer_id == head.c.consumer_id,
        behind.c.subject_ref == head.c.subject_ref,
        behind.c.position > head.c.position,
        behind.c.state.not_in((*_SETTLED, FAILED)),
    )


def list_failed_deliveries(
    conn: Connection, *, limit: int
) -> tuple[FailedDeliveryRow, ...]:
    """The most recently failed deliveries, newest ``completed_at`` first, capped at
    ``limit``, each with the number of deliveries it holds back.

    ``blocked_count`` is a correlated count per listed head, so its cost is bounded by
    ``limit`` heads, each read through the ``(consumer_id, subject_ref, position)``
    ordering index.
    """
    head = t.event_delivery.alias("head")
    blocked = (
        select(func.count())
        .select_from(t.event_delivery)
        .where(_blocked_behind(head))
        .scalar_subquery()
    )
    rows = conn.execute(
        select(
            head.c.event_id,
            head.c.consumer_id,
            head.c.attempts,
            head.c.last_error,
            head.c.completed_at,
            blocked.label("blocked_count"),
        )
        .where(head.c.state == FAILED)
        .order_by(head.c.completed_at.desc())
        .limit(limit)
    )
    return tuple(
        FailedDeliveryRow(
            event_id=row.event_id,
            consumer_id=str(row.consumer_id),
            attempts=int(row.attempts),
            last_error=None if row.last_error is None else str(row.last_error),
            completed_at=row.completed_at,
            blocked_count=int(row.blocked_count),
        )
        for row in rows
    )


@dataclass(frozen=True, slots=True)
class DeliveryFailureCounts:
    """The delivery half of ``core.work.failure_summary``: three numbers."""

    failed_count: int
    blocked_count: int
    oldest_failed_at: datetime | None


def delivery_failure_counts(conn: Connection) -> DeliveryFailureCounts:
    """How many deliveries are ``failed``, how many they hold back, and when the
    oldest of them failed.

    Two aggregate statements and no rows returned, which is what makes the summary a
    fixed-size read whatever the table holds. ``blocked_count`` counts a held-back row
    once even when two failed heads sit in front of it, because the ``EXISTS`` asks
    whether any does.
    """
    failed = conn.execute(
        select(func.count(), func.min(t.event_delivery.c.completed_at)).where(
            t.event_delivery.c.state == FAILED
        )
    ).one()
    head = t.event_delivery.alias("head")
    behind = t.event_delivery
    blocked = conn.execute(
        select(func.count())
        .select_from(behind)
        .where(
            behind.c.state.not_in((*_SETTLED, FAILED)),
            select(literal(1))
            .where(
                head.c.state == FAILED,
                head.c.consumer_id == behind.c.consumer_id,
                head.c.subject_ref == behind.c.subject_ref,
                head.c.position < behind.c.position,
            )
            .exists(),
        )
    ).scalar_one()
    return DeliveryFailureCounts(
        failed_count=int(failed[0]),
        blocked_count=int(blocked),
        oldest_failed_at=failed[1],
    )


def earliest_delivery_due_at(conn: Connection) -> datetime | None:
    """When this workspace's deliveries next need a visit, or ``None`` when none do.

    Mirrors ``jobs.earliest_due_at``: the earlier of the soonest pending
    ``next_attempt_at`` and the soonest leased ``lease_until`` — the first is work
    waiting to start, the second is the moment a lease lapses and its delivery becomes
    re-acquirable. ``LEAST`` ignores a null side, so a workspace with only one of the
    two still answers. ``delivered``, ``failed`` and ``skipped`` rows are neither.
    """
    value = conn.execute(
        select(
            func.least(
                func.min(t.event_delivery.c.next_attempt_at).filter(
                    t.event_delivery.c.state == PENDING
                ),
                func.min(t.event_delivery.c.lease_until).filter(
                    t.event_delivery.c.state == LEASED
                ),
            )
        )
    ).scalar_one_or_none()
    return value if isinstance(value, datetime) else None
