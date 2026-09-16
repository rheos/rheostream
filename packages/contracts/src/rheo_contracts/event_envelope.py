"""The event envelope: what a consumer is handed for one delivery of one event.

Source of truth: ``docs/architecture/intake-and-events.md`` § Events and the outbox
(FR 15). The document's own words are that the envelope "is the row above plus the
delivery context" — so the thirteen fields below are the ``core.outbox_event`` row
transcribed column for column, and ``consumer_id`` and ``attempt`` are the delivery
context, read off the ``core.event_delivery`` row that carries this attempt.

**Versioned per event type by ``schema_version``.** A producer may add optional fields
within a version; a removal or a meaning change is a new version, and a consumer
declares which versions it accepts.

That rule is also why ``subject_ref`` and ``actor_kind`` are plain ``str`` here rather
than the richer contract types sitting beside them
(:class:`~rheo_contracts.refs.RecordRef` and
:class:`~rheo_contracts.context.ActorKind`). An envelope is a wire shape read back out
of a ``jsonb`` column an older producer may have written, and a value this process
cannot parse must still arrive as a refusable delivery rather than an unconstructible
envelope. A consumer that wants the parsed form calls ``RecordRef.parse`` itself and
decides what a failure there means.

**Declared only, deliberately: this module defines the envelope and performs no I/O.**
The behaviour is named rather than deferred, because a claim about what does not exist
yet goes false the moment it does. Writing an outbox row and fanning it out to consumers
is ``rheo_core.events.publish``; delivering one is ``rheo_core.work.loop`` over
``rheo_core.storage.deliveries``.
"""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EventEnvelope(BaseModel):
    """One event, as one consumer receives it on one attempt."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    position: int
    """The outbox's ``bigserial``. Deliveries for one ``(consumer_id, subject_ref)``
    pair arrive in this order, so a consumer may read it to detect a gap but never to
    reorder: ordering is the deliverer's job, not the consumer's."""

    type: str
    schema_version: int
    source: str
    """``urn:rheo:module:<module_id>`` — the module that produced the event."""

    subject_ref: str
    subject_revision: int
    correlation_id: UUID
    """The originating operation's id. Every event one operation produces shares it,
    which is what ties a fan-out back to the single call that caused it."""

    causation_id: UUID | None = None
    """The event that led to this one, when there is one."""

    actor_kind: str
    actor_id: UUID | None = None
    occurred_at: datetime
    data: Mapping[str, object]
    """The declared event model **serialised**, which is the document's own word for it
    and the reason this is a mapping rather than a ``BaseModel``. The envelope does not
    know the event type's declared model and so cannot validate against it; the consumer
    that declares which ``schema_version`` it accepts is the one that can. The column is
    ``jsonb`` and is never queried, only delivered."""

    consumer_id: str
    attempt: int
    """Which attempt this is, counting from one — the value ``event_delivery.attempts``
    holds for this delivery. A consumer sees it so that a side effect it cannot make
    idempotent is at least distinguishable from a first run."""
