"""Outbox retention: the daily sweep's deletes, and the horizon replay gates on.

Called by ``core.retention_sweep`` (``rheo_core.work.schedules.run_retention_sweep``)
on every workspace, and read by ``core.work.replay``
(``rheo_core.work.operations.replay_handler``). ``intake-and-events.md`` § Retention
is the specification.

**What the sweep deletes.** An outbox event older than ``work.outbox_retention_days``
whose deliveries are all ``delivered`` or ``skipped``, or which never had one, goes
with those deliveries and every consumer's ``consumer_processed`` ledger row for it.

**What it never deletes.** An event with a ``pending``, ``leased`` or ``failed``
delivery stays, and so does that delivery: a ``failed`` row is an open item for a
person and the head of its subject queue, and the other two are work in motion. Two
guards hold this, the candidate query and then the event delete itself, which removes
an event only when no delivery row of any state is left for it in this transaction.
So a sweep never leaves a delivery whose event is gone, which the worker would have to
treat as a fault.

**Why that is stable while the sweep runs.** Outside replay, nothing moves a
``delivered`` or ``skipped`` row to another state, and nothing but replay adds a
delivery to an event that already committed (``publish`` writes the event and its
fan-out in one transaction). Replay is the one writer that does both, and it is
serialised against the sweep on ``core.outbox_retention``'s single row: the sweep
holds it ``FOR UPDATE`` from before its first read until it commits, and replay reads
it ``FOR SHARE`` before its gap check. Neither takes a table lock, so deliveries keep
flowing while a sweep runs.

**Why the record and not ``min(position)``.** A stuck event survives while newer ones
around it are deleted, so the smallest position still present sits below holes. The
sweep records the highest position it has deleted, and replay refuses anything at or
below it.

Does not commit; the sweep's and the replay's own units of work own that. Does not
read the process clock: ``now`` and the horizon are the caller's.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import Connection, delete, literal, select, update

from rheo_core.events.deliveries import DELIVERED, SKIPPED
from rheo_core.storage import work_tables as t
from rheo_core.storage.outbox_retention_tables import (
    OUTBOX_RETENTION_ROW_ID,
    outbox_retention,
)

OUTBOX_RETENTION_DAYS_KEY: Final = "work.outbox_retention_days"

BATCH_EVENTS: Final = 1000
"""Events one delete statement takes. Small enough that one batch's three deletes are
short; large enough that a day of ordinary traffic is one or two batches."""

MAX_BATCHES: Final = 20
"""Batches one sweep runs before it stops. A backlog past this many events waits for
the next daily sweep rather than holding one transaction open for longer."""

_TERMINAL: Final = (DELIVERED, SKIPPED)


class OutboxRetentionMissing(RuntimeError):
    """``core.outbox_retention`` has no row. The migration writes it, so a database
    without it is not at head; deleting anything then would leave replay ungated."""


@dataclass(frozen=True, slots=True)
class OutboxPurge:
    """What one sweep removed, and the recorded horizon after it.

    ``swept_through`` is the highest position ever deleted, this sweep or an earlier
    one, or ``None`` when no sweep has deleted anything yet.
    """

    events: int
    deliveries: int
    ledger_rows: int
    swept_through: int | None


def swept_through_position(conn: Connection) -> int | None:
    """The highest outbox position any sweep has deleted, read ``FOR SHARE``.

    Replay's horizon read. The share lock waits for a sweep that holds the row and is
    held until the caller commits, so a sweep cannot move the horizon between replay's
    gap check and its writes. Raises :class:`OutboxRetentionMissing` when the row is
    absent rather than answering ``None``, which would read as "nothing swept".
    """
    row = conn.execute(
        select(outbox_retention.c.swept_through_position)
        .where(outbox_retention.c.id == OUTBOX_RETENTION_ROW_ID)
        .with_for_update(read=True)
    ).one_or_none()
    if row is None:
        raise OutboxRetentionMissing("core.outbox_retention has no row")
    value = row.swept_through_position
    return None if value is None else int(value)


def purge_outbox(
    conn: Connection,
    *,
    now: datetime,
    occurred_before: datetime,
    checkpoint: Callable[[], None],
    batch_events: int = BATCH_EVENTS,
    max_batches: int = MAX_BATCHES,
) -> OutboxPurge:
    """Delete settled outbox events older than ``occurred_before``, oldest first.

    The caller passes ``now - work.outbox_retention_days``. ``checkpoint`` runs before
    each batch; the sweep passes its cancellation token's, so a cancelled sweep stops
    between batches and its transaction rolls back whole.
    """
    row = conn.execute(
        select(outbox_retention.c.swept_through_position)
        .where(outbox_retention.c.id == OUTBOX_RETENTION_ROW_ID)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise OutboxRetentionMissing("core.outbox_retention has no row")
    recorded = row.swept_through_position
    swept: int | None = None if recorded is None else int(recorded)

    unsettled = (
        select(literal(1))
        .where(
            t.event_delivery.c.event_id == t.outbox_event.c.id,
            t.event_delivery.c.state.not_in(_TERMINAL),
        )
        .exists()
    )
    any_delivery = (
        select(literal(1))
        .where(t.event_delivery.c.event_id == t.outbox_event.c.id)
        .exists()
    )
    events = deliveries = 0
    deleted_ids: list[UUID] = []
    for _ in range(max_batches):
        checkpoint()
        candidates: list[UUID] = list(
            conn.execute(
                select(t.outbox_event.c.id)
                .where(t.outbox_event.c.occurred_at < occurred_before, ~unsettled)
                .order_by(t.outbox_event.c.position)
                .limit(batch_events)
            ).scalars()
        )
        if not candidates:
            break
        deliveries += int(
            conn.execute(
                delete(t.event_delivery).where(
                    t.event_delivery.c.event_id.in_(candidates),
                    t.event_delivery.c.state.in_(_TERMINAL),
                )
            ).rowcount
        )
        gone = conn.execute(
            delete(t.outbox_event)
            .where(t.outbox_event.c.id.in_(candidates), ~any_delivery)
            .returning(t.outbox_event.c.id, t.outbox_event.c.position)
        ).all()
        if gone:
            events += len(gone)
            deleted_ids.extend(one.id for one in gone)
            highest = max(int(one.position) for one in gone)
            swept = highest if swept is None else max(swept, highest)
        if len(candidates) < batch_events:
            break

    ledger_rows = 0
    if deleted_ids:
        ledger_rows = int(
            conn.execute(
                delete(t.consumer_processed).where(
                    t.consumer_processed.c.event_id.in_(deleted_ids)
                )
            ).rowcount
        )
        conn.execute(
            update(outbox_retention)
            .where(outbox_retention.c.id == OUTBOX_RETENTION_ROW_ID)
            .values(swept_through_position=swept, swept_at=now)
        )
    return OutboxPurge(
        events=events,
        deliveries=deliveries,
        ledger_rows=ledger_rows,
        swept_through=swept,
    )
