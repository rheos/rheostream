"""``core.work.failures``'s models and handler, beside the repositories they read.

Two repositories now: ``work.jobs.list_failed_jobs`` and
``events.deliveries.list_failed_deliveries``, one per collection of the response.

The declaration and the registration live in ``operations/core_ops.py``; the
models and the handler live **here**, and the split is an import-direction
constraint rather than a preference. ``rheo_core.operations``'s own
``__init__.py`` imports ``core_ops`` as its first statement, and ``core_ops``
imports this module at module level, so **nothing in this module may import from
``rheo_core.operations``** — the reverse import would close a real cycle and each
module would be half-initialised by the time the other needed it. Nothing here
needs one: ``dispatch()`` is what authorizes the call, validates the input, seals
the unit of work and turns a refusal into an outcome, and a read handler raises
nothing on its happy path.

``rheo_core.work.jobs``, ``rheo_core.events`` and pydantic are therefore the whole
dependency surface, plus ``WorkspaceContext`` and ``UnitOfWork`` for the handler's
own signature.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from rheo_contracts import WorkspaceContext

from rheo_core.events import list_failed_deliveries
from rheo_core.storage.backend import UnitOfWork
from rheo_core.work.jobs import list_failed_jobs


class FailedJob(BaseModel):
    """One failed job as the operation publishes it.

    ``rheo_core.work.jobs.FailedJobRow`` field for field, with that row's ``id``
    published as ``job_id``: the response is about jobs and nothing else in it
    carries an id, so the longer name costs nothing and reads unambiguously in a
    generated client.
    """

    model_config = ConfigDict(frozen=True)

    job_id: UUID
    kind: str
    attempts: int
    max_attempts: int
    last_error: str | None
    finished_at: datetime | None


class FailedDelivery(BaseModel):
    """One failed event delivery as the operation publishes it.

    ``rheo_core.events.deliveries.FailedDeliveryRow`` field for field, and
    deliberately **not** :class:`FailedJob`'s field list: ``core.event_delivery``
    has no ``max_attempts`` column, because a delivery's budget is the
    ``work.max_attempts`` setting rather than a per-row value, and publishing a
    field the row does not hold would mean inventing one here.

    The identity is the composite key the row is actually keyed by, both halves
    published: one event has one delivery per consumer, so ``event_id`` alone does
    not name a delivery.
    """

    model_config = ConfigDict(frozen=True)

    event_id: UUID
    consumer_id: str
    attempts: int
    last_error: str | None
    completed_at: datetime | None


class FailureList(BaseModel):
    """The workspace's most recently failed jobs and deliveries, newest first.

    ``jobs`` is a **named collection field rather than a bare list**, and
    ``deliveries`` is what that shape was chosen for: it is added **beside**
    ``jobs``, additively, so a client generated against the document before it
    keeps reading ``jobs`` unchanged. A top-level ``list[FailedJob]`` would have
    made this addition a breaking change to the response's own type instead of a
    new field a reader may ignore.

    Each collection is ordered and capped on its own, newest first, by the same
    ``limit``: they are two independent lists of the most recent failures, not two
    halves of one merged ordering.
    """

    model_config = ConfigDict(frozen=True)

    jobs: list[FailedJob]
    deliveries: list[FailedDelivery]


class FailureListInput(BaseModel):
    """How many of the most recent failures to return.

    ``extra = "ignore"`` mirrors ``core_ops.py``'s ``_IGNORE_EXTRA``: a payload
    that also names another workspace has those keys dropped and the operation
    proceeds against the context's own workspace.

    **``limit`` is bounded, not a bare ``int``.** A bare ``int`` lets ``-1`` reach
    the ``SELECT``, where Postgres raises *LIMIT must not be negative*;
    ``dispatch`` catches that as a handler exception and refuses ``handler_failed``
    carrying only the exception's class name, when it is an input defect that
    belongs in the ``input_invalid`` refusal pydantic already produces for a
    constraint violation. ``500`` is a choice rather than a transcription: the use
    is one screen of failures, and nothing prunes this table in release one, so an
    unbounded ``limit`` is an unbounded read that grows for the life of the
    deployment.
    """

    model_config = ConfigDict(extra="ignore")

    limit: int = Field(default=50, ge=1, le=500)


def failures_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, input_model: FailureListInput
) -> FailureList:
    """The most recently failed jobs and deliveries of the context's own workspace.

    The signature mirrors ``core_ops.py``'s ``_workspace_status`` exactly, a plain
    :class:`~rheo_core.storage.backend.UnitOfWork` included: this is a *registered
    operation* handler reached through ``dispatch()``, which is itself what hands
    the handler the sealed ``HandlerUnitOfWork`` view — a different call path from
    the job-kind handlers the worker loop runs, and not one this module seals for
    itself.

    The workspace is the context's. ``uow.connection`` is already routed to it, so
    no input field names one and none can.

    ``limit`` bounds each collection rather than their sum: the two lists answer
    two different questions, and a shared budget would let a burst of failed jobs
    hide every failed delivery.
    """
    jobs = list_failed_jobs(uow.connection, limit=input_model.limit)
    deliveries = list_failed_deliveries(uow.connection, limit=input_model.limit)
    return FailureList(
        jobs=[
            FailedJob(
                job_id=row.id,
                kind=row.kind,
                attempts=row.attempts,
                max_attempts=row.max_attempts,
                last_error=row.last_error,
                finished_at=row.finished_at,
            )
            for row in jobs
        ],
        deliveries=[
            FailedDelivery(
                event_id=delivery.event_id,
                consumer_id=delivery.consumer_id,
                attempts=delivery.attempts,
                last_error=delivery.last_error,
                completed_at=delivery.completed_at,
            )
            for delivery in deliveries
        ],
    )
