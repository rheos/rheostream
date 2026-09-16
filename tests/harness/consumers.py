"""The deduplicating test consumer (``docs/requirements/build-plan.md:55-56``): what
one consumer records when the worker delivers an event to it.

Test-profile scaffolding, like every other module in this package. It is never
registered by anything under ``packages/`` or ``apps/``: the worker's composition
root ships an empty :class:`~rheo_core.events.ConsumerRegistry`, and a test builds
its own from :func:`registry` here.

**It records two things, and they must not share a transaction.**

1. **The domain effect** — a ``harness.recorded_event`` row, written on the
   :class:`~rheo_core.storage.backend.HandlerUnitOfWork` the delivery drain handed
   the handler. It therefore lives in the delivery's own transaction and is rolled
   back with it, which is what proves "a handler that raises leaves none of its own
   effects".
2. **"an invocation started"** — a ``harness.consumer_invocation`` row written **out
   of band**, on a second connection that commits immediately, before the handler
   body does anything else. This is what a call count must be read from.

   The reason is specific rather than stylistic. If the handler is invoked a *second*
   time for one event, its domain effect and the ``core.consumer_processed`` insert
   that follows it are in the same transaction, and that insert's primary-key conflict
   rolls the whole transaction back — taking an in-transaction invocation counter with
   it and hiding the double invocation from any test that reads the database
   afterwards. A counter committed the moment the handler body starts is not defeated
   by a later rollback in the main transaction, and it is equally not defeated by a
   process that dies before the transaction ends, which is what lets the parent
   process of ``tests/postgres/test_event_delivery.py``'s kill test read the
   consumer's state back after the child is gone.

**The second connection comes from the delivery's own engine.**
``uow.connection.engine.connect()`` opens an independent connection out of the pool
that engine already owns. Safe here specifically because that pool is sized
``pool_size = storage.pool_max_connections`` (default 5, ``settings/schema.py:421-427``)
with ``max_overflow = 0`` (``storage/pools.py``'s ``engine_for``), and the delivery
drain's own transaction holds exactly one of those while the handler runs. It is
opened, written, committed and closed inside :func:`_mark_invocation`; nothing holds
it past that one write.

**Neither table carries a natural-key primary key**, deliberately. A unique constraint
on ``event_id`` would make the *domain* write raise on a second invocation, which
would mask which guard actually stopped the repeat: the point of these tables is to
observe a double invocation, not to prevent one. ``core.consumer_processed``'s primary
key is the guard, and it lives in the shipped schema.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from rheo_contracts import EventEnvelope
from rheo_core.events import ConsumerHandler, ConsumerRegistry, ConsumerSubscription
from rheo_core.refs import uuid7
from rheo_core.storage.backend import HandlerUnitOfWork
from sqlalchemy import (
    Column,
    Connection,
    DateTime,
    Integer,
    Table,
    Text,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from harness.records import HARNESS_SCHEMA, harness_metadata

CONSUMER_ID: Final = "harness.recorder"
"""The stable identifier tests reference this consumer by.

It is what the ``core.event_delivery`` row carries, which is why the kill test's child
and parent processes need no shared registry object — only this string.
"""

EVENT_TYPE: Final = "harness.note.written"
MODULE_ID: Final = "harness"
"""The owning module the fan-out tests against ``ctx.enabled_modules``, so a workspace
publishing to this consumer has ``harness`` enabled (``harness.registry``'s
``enable_harness_module``). Not the reserved core segment: this is module-shipped
scaffolding and declaring it core-owned to skip one row of setup would make the
fan-out's enabled-module test vacuous wherever this consumer is used.
"""

recorded_event = Table(
    "recorded_event",
    harness_metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("consumer_id", Text, nullable=False),
    Column("event_id", PG_UUID(as_uuid=True), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)
"""The domain effect, written inside the delivery's transaction."""

consumer_invocation = Table(
    "consumer_invocation",
    harness_metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("consumer_id", Text, nullable=False),
    Column("event_id", PG_UUID(as_uuid=True), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
)
"""The out-of-band marker, committed on its own connection as the handler starts."""


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    event_id: UUID
    attempt: int


def ensure_consumer_tables(conn: Connection) -> None:
    """Create both tables in the connection's database if they are absent."""
    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {HARNESS_SCHEMA}"))
    recorded_event.create(conn, checkfirst=True)
    consumer_invocation.create(conn, checkfirst=True)


def _mark_invocation(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
    """Commit "this handler body started" on a second connection, out of band.

    Opened, written, committed and closed here; the delivery's own transaction is
    untouched, so this row survives both the rollback a raising handler causes and the
    rollback a ``consumer_processed`` conflict causes.
    """
    with uow.connection.engine.connect() as side:
        side.execute(
            insert(consumer_invocation).values(
                id=uuid7(),
                consumer_id=envelope.consumer_id,
                event_id=envelope.id,
                attempt=envelope.attempt,
                started_at=datetime.now(UTC),
            )
        )
        side.commit()


def record_delivery(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
    """The ratified consumer: mark the invocation, then record the identifier.

    The marker is first because it stands for "the handler body started", and a
    handler that raises on its next statement must still have left one.
    """
    _mark_invocation(uow, envelope)
    uow.connection.execute(
        insert(recorded_event).values(
            id=uuid7(),
            consumer_id=envelope.consumer_id,
            event_id=envelope.id,
            attempt=envelope.attempt,
            recorded_at=datetime.now(UTC),
        )
    )


def handler_that_raises(message: str) -> ConsumerHandler:
    """The same consumer, raising after it has written its domain effect.

    Both halves matter to the test that uses it: the marker proves the body ran, and
    the domain row proves the rollback took a real write with it rather than finding
    nothing to undo.
    """

    def _raise_after_recording(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
        record_delivery(uow, envelope)
        raise RuntimeError(message)

    return _raise_after_recording


def subscription(handler: ConsumerHandler = record_delivery) -> ConsumerSubscription:
    """This consumer's subscription, with ``handler`` swapped in for the failure
    cases."""
    return ConsumerSubscription(
        consumer_id=CONSUMER_ID,
        event_type=EVENT_TYPE,
        module_id=MODULE_ID,
        replay_safe=True,
        handler=handler,
    )


def registry(handler: ConsumerHandler = record_delivery) -> ConsumerRegistry:
    """A registry holding this one consumer, built fresh per caller."""
    built = ConsumerRegistry()
    built.register(subscription(handler))
    return built


def recorded_events(conn: Connection) -> tuple[RecordedEvent, ...]:
    """Every identifier this consumer has recorded, oldest first."""
    rows = conn.execute(
        select(recorded_event.c.event_id, recorded_event.c.attempt).order_by(
            recorded_event.c.recorded_at, recorded_event.c.id
        )
    )
    return tuple(
        RecordedEvent(event_id=row.event_id, attempt=int(row.attempt)) for row in rows
    )


def invocation_count(conn: Connection, *, event_id: UUID | None = None) -> int:
    """How many times this consumer's handler body has started.

    Read from the database rather than from a return value or process memory: the
    kill test's child process is gone by the time the parent asserts, and a counter
    in the delivery's own transaction would be rolled back by the very failures this
    count exists to observe.
    """
    statement = select(func.count()).select_from(consumer_invocation)
    if event_id is not None:
        statement = statement.where(consumer_invocation.c.event_id == event_id)
    return int(conn.execute(statement).scalar_one())
