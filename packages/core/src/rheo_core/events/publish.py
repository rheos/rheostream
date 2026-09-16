"""The outbox write and its fan-out: one ``core.outbox_event`` row, and one
``core.event_delivery`` row per subscribed consumer whose module is enabled here.

**A free function taking the caller's ``ctx`` and ``uow``, not a method on
``UnitOfWork`` (D1).** ``tests/postgres/test_isolation.py:194-201`` pins that class's
public surface to exactly ``{commit, connection, rollback, verify_database}`` and its
slots to exactly four, asserting neither mentions an outbox — a ratified boundary
guard, and honouring one beats editing it. Every other repository in this tree is a
free function over the caller's connection for the same reason, and the free function
points ``events -> storage`` without introducing an edge back into a module almost
everything imports.

Nothing semantic is given up: the insert still happens inside the caller's one
transaction, which is the property exactly-once delivery actually rests on, and
``HandlerUnitOfWork`` is a ``UnitOfWork``, so a module handler publishes through the
same call.

**``now`` and ``consumers`` are required keywords with no defaults (D1a).** Two NOT
NULL timestamp columns are written here — ``outbox_event.occurred_at``
(``work_tables.py:89``) and ``event_delivery.next_attempt_at`` (``:105``) — and
``WorkspaceContext`` carries no clock. A default of ``datetime.now(UTC)`` inside this
function would make a delivery's first-attempt instant untestable without sleeping,
the exact cost ``work/jobs.py``'s own docstring says this discipline exists to
prevent. ``consumers`` has no module-level default because there is no process-wide
registry to fall back on; a test builds its own.

**``correlation_id`` is derived here, not supplied.** The ratified column is "the
originating operation's id" (``intake-and-events.md:301``) and it is NOT NULL, so it
must always resolve, and :class:`NewEvent` therefore does not carry it. It resolves in
two steps, in this order:

1. **The originating operation's id, when there is one.** A handler publishing inside
   a ``long_running`` dispatch is handed a ``HandlerUnitOfWork`` carrying the id
   ``dispatch()`` minted, and that id is what the ratified sentence actually names.
2. **``ctx.request_id`` otherwise**, which is every other publish: no operation record
   is minted for a declaration that is not ``long_running``, so the request id stays
   the only identifier the causing call has. Recorded as the standing deviation for
   that case rather than as a temporary one — it is what a non-long-running operation's
   events will always carry.

The branch reads the *unit of work*, not the context, because the id lives on the view
the dispatcher built for this one call and ``WorkspaceContext`` is frozen at the
boundary, long before anything is minted.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from rheo_contracts import WorkspaceContext, is_reserved_module
from sqlalchemy import insert

from rheo_core.events.consumers import ConsumerRegistry
from rheo_core.events.deliveries import PENDING
from rheo_core.refs import uuid7
from rheo_core.storage import work_tables as t
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.work_index import mark_work_due

SOURCE_PREFIX: Final = "urn:rheo:module:"
"""``outbox_event.source`` is ``urn:rheo:module:<module_id>``
(``intake-and-events.md:293``), where the module is the event type's first segment."""


@dataclass(frozen=True, slots=True)
class NewEvent:
    """What a producer hands :func:`publish`. Six fields, no more, no fewer.

    It carries **no instant** and **no ``correlation_id``**: both are this module's
    concern rather than the caller's, for the reasons in the module docstring.
    ``source`` is likewise derived, from ``type``, and ``actor_kind``/``actor_id``
    come from the context — so the only columns a producer chooses are these.

    ``data`` is the declared event model **serialised**, which is why it is a mapping
    rather than a model: the outbox does not know the event type's declared model and
    so cannot validate against it. The column is ``jsonb`` and is never queried, only
    delivered.
    """

    type: str
    schema_version: int
    subject_ref: str
    subject_revision: int
    data: dict[str, object]
    causation_id: UUID | None = None


def publish(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    event: NewEvent,
    *,
    now: datetime,
    consumers: ConsumerRegistry,
) -> UUID:
    """Write one outbox row and fan it out. Returns the new event's id.

    **Commits nothing and rolls back nothing.** Both writes run inside the caller's
    own unit of work, so a caller that rolls back afterwards finds neither the outbox
    row nor any delivery row — which is what makes a state change and its event
    delivery one transaction.

    A consumer receives a delivery row when it is subscribed to ``event.type`` **and**
    its module is the core segment or is in ``ctx.enabled_modules`` at this moment. A
    consumer whose module is not enabled gets no row and never sees the event.

    **An event nobody consumes is not an error.** It still writes its one outbox row
    and completes with zero delivery rows: the outbox is the record of what happened,
    and what happened does not depend on who was listening.
    """
    connection = uow.connection
    event_id = uuid7()
    module_id = event.type.split(".", 1)[0]
    # The originating operation's id when this publish is inside a ``long_running``
    # dispatch, and the request id otherwise. See the module docstring.
    correlation_id = (
        uow.operation_id
        if isinstance(uow, HandlerUnitOfWork) and uow.operation_id is not None
        else ctx.request_id
    )
    position = connection.execute(
        insert(t.outbox_event)
        .values(
            id=event_id,
            type=event.type,
            schema_version=event.schema_version,
            source=f"{SOURCE_PREFIX}{module_id}",
            subject_ref=event.subject_ref,
            subject_revision=event.subject_revision,
            correlation_id=correlation_id,
            causation_id=event.causation_id,
            actor_kind=ctx.actor.kind.value,
            actor_id=ctx.actor.id,
            occurred_at=now,
            data=event.data,
        )
        .returning(t.outbox_event.c.position)
    ).scalar_one()

    deliveries = [
        {
            "event_id": event_id,
            "consumer_id": subscription.consumer_id,
            "subject_ref": event.subject_ref,
            "position": position,
            "state": PENDING,
            "attempts": 0,
            "next_attempt_at": now,
            "lease_owner": None,
            "lease_until": None,
            "last_error": None,
            "completed_at": None,
        }
        for subscription in consumers.for_type(event.type)
        if is_reserved_module(subscription.module_id)
        or subscription.module_id in ctx.enabled_modules
    ]
    if deliveries:
        connection.execute(insert(t.event_delivery), deliveries)
    return event_id


def mark_due_after_publish(workspace_id: UUID, at: datetime) -> None:
    """Mark ``workspace_id`` due in the control-plane index, in its own transaction.

    **Deliberately not called by :func:`publish`.** The outbox row lands in the
    workspace database and the mark lands in the control database; no transaction
    spans the two (``storage/work_index.py``'s own docstring makes the same point), so
    a publisher commits its own transaction first and calls this afterwards, exactly
    as ``work.jobs.enqueue`` does. A crash between them loses the mark, which is
    bounded by the reconcile floor rather than fixed by a transaction that cannot
    exist.

    **Its caller today is ``operations/dispatch.py``'s ``long_running`` success path**,
    through ``_mark_workspace_due``, which is the first one it has had. It was written
    for a publisher and shipped callerless in run 0c2, because that run shipped no
    production publisher; run 0c3 found the same shape one layer over — a handler that
    enqueued inside the dispatcher's workspace transaction and had no control-plane
    connection to mark with — and reached for this rather than writing a second copy of
    it. A publisher, when one ships, is still what the function was named for and calls
    it the same way.
    """
    with get_backend().control_engine.begin() as control:
        mark_work_due(control, workspace_id, at=at)
