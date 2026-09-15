"""``core.work.failures``'s models and handler, beside the repository they read.

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

``rheo_core.work.jobs`` and pydantic are therefore the whole dependency surface,
plus ``WorkspaceContext`` and ``UnitOfWork`` for the handler's own signature.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from rheo_contracts import WorkspaceContext

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


class FailureList(BaseModel):
    """The workspace's most recently failed jobs, newest first.

    ``jobs`` is a **named collection field rather than a bare list**, deliberately:
    0c2 adds sibling collections here — its own deliveries, its own unfinished
    work — beside ``jobs``, additively, and every client generated against
    today's document keeps reading ``jobs`` unchanged. A top-level
    ``list[FailedJob]`` would make that same addition a breaking change to the
    response's own type instead of a new field a reader may ignore.
    """

    model_config = ConfigDict(frozen=True)

    jobs: list[FailedJob]


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
    """The most recently failed jobs of the context's own workspace.

    The signature mirrors ``core_ops.py``'s ``_workspace_status`` exactly, a plain
    :class:`~rheo_core.storage.backend.UnitOfWork` included: this is a *registered
    operation* handler reached through ``dispatch()``, which is itself what hands
    the handler the sealed ``HandlerUnitOfWork`` view — a different call path from
    the job-kind handlers the worker loop runs, and not one this module seals for
    itself.

    The workspace is the context's. ``uow.connection`` is already routed to it, so
    no input field names one and none can.
    """
    rows = list_failed_jobs(uow.connection, limit=input_model.limit)
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
            for row in rows
        ]
    )
