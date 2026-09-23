"""Every job this module enqueues, and the one rule for when a memory gets an embed job.

**A provider, not only a strategy.** An embed job is written only when the workspace's
resolved strategy reads the dense index (``dense`` or ``hybrid``) **and** a provider
resolves. Strategy alone never enqueues: under the shipped defaults — ``provider =
none`` always, ``hybrid`` once it is offered — nothing is written at all, so no job row,
no worker wake, no attempt and no failure entry exists for a dense posture nobody
configured.

**Two callers, one helper.** A memory's text is written in two places: the one insert
``remember``, ``derive``, ``supersede`` and trusted acceptance all share
(``writes.write_memory``), and ``lifecycle.correct``, which rewrites the row in place
and never reaches that insert. Both call :func:`enqueue_embed_job`, so a corrected
memory is re-embedded on the same terms as a new one.

**The job commits with the memory; the provider runs after.** The row is written in the
writer's own transaction, so it exists exactly when the memory does, and the provider is
called by the worker once that transaction has committed.

**One more caller, ungated: the rebuild's leftovers.** A rebuild skips rows another
writer holds, and queues an embed job for each live memory its walk left unembedded
(:func:`enqueue_embed_jobs`), so a skipped row whose writer rolled back is still filled.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.work.jobs import enqueue_job

from rheo_recallatron.configuration import (
    EMBED_JOB_KIND,
    EMBED_MAX_ATTEMPTS,
    REBUILD_JOB_KIND,
    REBUILD_MAX_ATTEMPTS,
    STRATEGY_DENSE,
    STRATEGY_HYBRID,
)
from rheo_recallatron.embedding.protocol import EmbeddingProvider
from rheo_recallatron.embedding.registry import resolve_provider
from rheo_recallatron.retrieval.dispatch import resolve_strategy


def should_enqueue_embed(
    strategy_name: str, provider: EmbeddingProvider | None
) -> bool:
    """True only for a strategy that reads the dense index and a provider to fill it."""
    return provider is not None and strategy_name in (STRATEGY_DENSE, STRATEGY_HYBRID)


def enqueue_embed_job(
    ctx: WorkspaceContext, uow: UnitOfWork, *, memory_id: UUID, now: datetime
) -> bool:
    """Queue one embed job for ``memory_id`` when the workspace can use one.

    Answers whether it did. Called inside the writer's transaction, after the row it
    names has been written.
    """
    strategy_name = resolve_strategy(ctx, uow).name
    provider = resolve_provider()
    if not should_enqueue_embed(strategy_name, provider):
        return False
    enqueue_embed_jobs(uow, [memory_id], now=now)
    return True


def enqueue_embed_jobs(
    uow: UnitOfWork, memory_ids: Sequence[UUID], *, now: datetime
) -> int:
    """Queue one embed job per memory, with no strategy gate; answer how many.

    The one place an embed job row is written. :func:`enqueue_embed_job` reaches it
    through the gate above. The rebuild reaches it directly, for the live memories its
    walk could not embed because another writer held them at the time: the owner asked
    for this workspace's index to be filled, a provider resolved for that, and these
    rows are the part of the fill the walk had to leave to a job.
    """
    for memory_id in memory_ids:
        enqueue_job(
            uow.connection,
            kind=EMBED_JOB_KIND,
            payload={"memory_id": str(memory_id)},
            now=now,
            max_attempts=EMBED_MAX_ATTEMPTS,
        )
    return len(memory_ids)


def enqueue_rebuild_job(
    uow: UnitOfWork, *, operation_id: UUID, batch_size: int, now: datetime
) -> UUID:
    """Queue the one job a ``recallatron.embedding.rebuild`` dispatch runs as.

    ``operation_id`` goes on the job row, so the worker closes the operation record when
    the job finishes, **and** into the payload, because the worker hands a job no
    operation id of its own: inside the job, ``uow.operation_id`` is ``None``, and the
    payload is the only place the job learns which record to report progress on.
    """
    return enqueue_job(
        uow.connection,
        kind=REBUILD_JOB_KIND,
        payload={"operation_id": str(operation_id), "batch_size": batch_size},
        now=now,
        max_attempts=REBUILD_MAX_ATTEMPTS,
        operation_id=operation_id,
    )
