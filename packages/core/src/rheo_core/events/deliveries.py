"""Every read and write against ``core.event_delivery`` and
``core.consumer_processed``: the ordered lease, the three finishes, the requeue, the
dedup ledger, and the two queries a deliverer and a failure list need.

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
in :func:`lease_delivery`'s candidate set and nothing in this module moves a row out of
it, so a lapsed owner's :func:`fail_delivery` would strand its whole
``(consumer_id, subject_ref)`` queue behind a head that was in fact processed exactly
once — a permanent wrong state, not a window something later closes.

Every write is predicated and returns whether it applied, exactly as ``jobs.py:40-46``
requires: an unpredicated ``UPDATE`` here would move a delivery out from under the
worker that is running it.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import (
    Connection,
    and_,
    func,
    insert,
    literal,
    or_,
    select,
    tuple_,
    update,
)
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

    Five fields: the delivery's identity, its attempts, its error and its completion
    instant. Deliberately **not** a field-for-field copy of
    ``work.jobs.FailedJobRow`` — ``event_delivery`` has no ``max_attempts`` column,
    because a delivery's budget is the ``work.max_attempts`` setting rather than a
    per-row value.
    """

    event_id: UUID
    consumer_id: str
    attempts: int
    last_error: str | None
    completed_at: datetime | None


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

    **The write :func:`_held` matters most for.** ``failed`` is the one state this
    module never moves a row out of, and a failed head blocks every later delivery for
    its consumer and subject. A worker whose lease lapsed mid-handler writing this
    against the row its successor now holds would strand that queue permanently, on an
    event that was in fact processed exactly once.
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


def skip_delivery(
    conn: Connection, *, event_id: UUID, consumer_id: str, now: datetime
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

    ``event_delivery`` has no ``note`` column, so this takes none: the "with a note"
    language in ``intake-and-events.md`` describes the audit trail of the future
    operator-facing operation, not this repository row.

    **This ships with no caller.** The operator-facing operation that releases a
    blocked head is deferred to a later run; the function is where it belongs and is
    registered as callerless by design rather than smuggled in.
    """
    return _apply(
        conn,
        and_(_row(event_id, consumer_id), t.event_delivery.c.state == FAILED),
        state=SKIPPED,
        completed_at=now,
        lease_owner=None,
        lease_until=None,
    )


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


def list_failed_deliveries(
    conn: Connection, *, limit: int
) -> tuple[FailedDeliveryRow, ...]:
    """The most recently failed deliveries, newest ``completed_at`` first, capped at
    ``limit``."""
    rows = conn.execute(
        select(
            t.event_delivery.c.event_id,
            t.event_delivery.c.consumer_id,
            t.event_delivery.c.attempts,
            t.event_delivery.c.last_error,
            t.event_delivery.c.completed_at,
        )
        .where(t.event_delivery.c.state == FAILED)
        .order_by(t.event_delivery.c.completed_at.desc())
        .limit(limit)
    )
    return tuple(
        FailedDeliveryRow(
            event_id=row.event_id,
            consumer_id=str(row.consumer_id),
            attempts=int(row.attempts),
            last_error=None if row.last_error is None else str(row.last_error),
            completed_at=row.completed_at,
        )
        for row in rows
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
