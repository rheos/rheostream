"""The fill, and ``recallatron.embedding.rebuild``'s job: one self-completing walk.

Two context-free callables and the job body. :func:`fill_batch` embeds one id-ordered
batch of live memories lacking a vector for the provider's model;
:func:`fill_missing_embeddings` loops it to completion in the caller's unit of work, and
is what a harness calls to fill a workspace without dispatching anything. Neither takes
a ``WorkspaceContext``: their callers are jobs and tests, and a job holds none.

**The rebuild is one job, never a chain.** The core closes an operation record when the
job carrying its id succeeds, so a continuation job would report its progress into a
closed record and silently no-op, and two jobs on one record is the producer defect the
worker logs. So :func:`run_embedding_rebuild_job` prunes, then walks every batch inside
its own single handler invocation.

**The tradeoff that buys, stated.** The prune, every batch's inserts and every progress
write sit in the job's one transaction, because a handler may not commit. So progress
becomes visible to a reader of the operation record *at commit*, not batch by batch; a
crash or a lost lease mid-walk rolls the whole walk back; and a rerun is safe rather
than incremental, because the fill selects only memories lacking a row — a retried job
starts from whatever last committed, which after a rollback is nothing. Between batches
the job checkpoints, which is what keeps a long walk from being taken for a dead one.

**The same one transaction holds every row the walk has read, ``FOR SHARE``, until it
commits** — the repository's vector-write handshake, which is what stops a correction
landing mid-walk from leaving the old text's vector behind. The cost is real and it is
stated here: a correction, invalidation or deletion of a memory the walk has already
embedded waits for the whole rebuild to commit, and it waits holding the workspace
lifecycle lock, so the workspace's other writes queue behind it for the rest of the
walk. The fill skips a row another writer holds rather than waiting for it, so the
rebuild itself never waits on a writer and the two cannot deadlock.

**The operation id comes from the payload.** The worker builds a job's unit of work
with no operation id, so inside this handler ``uow.operation_id`` is ``None`` and a
progress write against it would match no record. The dispatch handler, which does hold
the id, writes it into the payload.

**The closing ``ANALYZE`` serves the dense path, and that is all it is for.** It
refreshes the planner's view of a table the walk just touched. It is not what keeps the
lexical query builder's statistics present: this operation refuses outright under the
shipped ``provider = none``, which is exactly the state that concern is about, and
migration ``0003_dense_retrieval``'s autovacuum setting is what addresses it.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.operations.records import set_progress
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.work.cancellation import CancellationToken

from rheo_recallatron.configuration import (
    EMBED_INPUT_VERSION,
    EMBEDDING_BATCH_SIZE_MAXIMUM,
    EMBEDDING_BATCH_SIZE_MINIMUM,
)
from rheo_recallatron.embedding.protocol import EmbeddingProvider
from rheo_recallatron.embedding.registry import resolve_provider
from rheo_recallatron.refusals import EMBEDDING_PROVIDER_UNAVAILABLE
from rheo_recallatron.retrieval.dense import DenseStrategy
from rheo_recallatron.retrieval.protocol import IndexItem
from rheo_recallatron.storage.repository import (
    EmbeddingCoverage,
    analyze_memory,
    delete_all_memory_embeddings,
    delete_embeddings_for_other_models,
    embedding_coverage,
    get_embedding_state,
    list_memories_missing_embedding,
    set_embedding_state,
)


class EmbeddingRebuildPayload(BaseModel):
    """The operation record to report on, and the batch size the dispatch read.

    Both are read by the dispatch handler, which holds the context and the operation
    id; the job holds neither, so they travel here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: UUID
    batch_size: int = Field(
        ge=EMBEDDING_BATCH_SIZE_MINIMUM, le=EMBEDDING_BATCH_SIZE_MAXIMUM
    )


class ProgressUnrecorded(RuntimeError):
    """A progress write matched no open operation record — a producer defect.

    The record is opened by the dispatch that enqueued this job and closed only when
    the job ends, so while the job runs a ``False`` from the write means the id it was
    handed names no open record. Failing loudly beats a walk that reports into nothing.
    """


def fill_batch(
    uow: UnitOfWork,
    *,
    provider: EmbeddingProvider,
    after: UUID | None,
    limit: int,
) -> tuple[int, UUID | None]:
    """Embed one batch of live memories lacking a row for ``provider.model_id``.

    Answers ``(rows written, next cursor)``: the cursor is the batch's last id, or
    ``None`` when the batch came back short and there is nothing after it.

    The batch is read ``FOR SHARE`` with any row another writer holds skipped, and the
    locks stay until the caller's transaction ends, so every vector written here is of
    text its row still holds when the vector commits (the repository's vector-write
    handshake). A skipped row is being changed right now and is left for that change's
    own embed job.
    """
    if limit < 1:
        raise ValueError("a fill batch holds at least one memory")
    rows = list_memories_missing_embedding(
        uow.connection, model_id=provider.model_id, after=after, limit=limit
    )
    written = DenseStrategy().index_many(
        uow,
        [IndexItem(row.id, row.title, row.body) for row in rows],
        provider=provider,
    )
    return written, (rows[-1].id if len(rows) == limit else None)


def fill_missing_embeddings(
    uow: UnitOfWork, *, provider: EmbeddingProvider, batch_size: int
) -> int:
    """Give every live memory a row for ``provider.model_id``; answer how many it wrote.

    In the caller's unit of work, which it neither commits nor rolls back. A memory
    another writer holds at the moment its batch is read is skipped (see
    :func:`fill_batch`); with no concurrent writer, none is.
    """
    written = 0
    after: UUID | None = None
    while True:
        batch, after = fill_batch(uow, provider=provider, after=after, limit=batch_size)
        written += batch
        if after is None:
            return written


def run_embedding_rebuild_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """Prune, fill to completion with progress, analyze. One job, one transaction."""
    if not isinstance(payload, EmbeddingRebuildPayload):
        raise TypeError(
            f"expected an EmbeddingRebuildPayload, got {type(payload).__name__}"
        )
    token.checkpoint()
    provider = resolve_provider()
    if provider is None:
        # The dispatch refused unless a provider resolved, so this is the
        # configuration changing under a queued rebuild. An owner asked for this
        # explicitly; ending it as a success would tell them it happened.
        raise OperationRefused(
            EMBEDDING_PROVIDER_UNAVAILABLE,
            "no embedding provider resolves any more; nothing was rebuilt",
        )
    conn = uow.connection
    delete_embeddings_for_other_models(conn, model_id=provider.model_id)
    state = get_embedding_state(conn)
    if state is None or state.embed_input_version != EMBED_INPUT_VERSION:
        delete_all_memory_embeddings(conn)
        set_embedding_state(conn, embed_input_version=EMBED_INPUT_VERSION)

    start = embedding_coverage(conn, model_id=provider.model_id)
    missing_at_start = start.live - start.embedded
    if missing_at_start == 0:
        _report(uow, payload, provider, coverage=start, fraction=1.0)
        token.checkpoint()
    else:
        written = 0
        after: UUID | None = None
        while True:
            batch, after = fill_batch(
                uow, provider=provider, after=after, limit=payload.batch_size
            )
            written += batch
            # Complete is 1.0 whatever the arithmetic says: a memory invalidated
            # mid-walk is one fewer to embed, and one written after the walk began is
            # one more, and neither makes a finished walk less than finished.
            fraction = 1.0 if after is None else min(written / missing_at_start, 1.0)
            # The start count plus what this walk wrote, not a recount: a coverage
            # query per batch is a full scan of the table each time, and the walk
            # already knows what it has added.
            _report(
                uow,
                payload,
                provider,
                coverage=EmbeddingCoverage(
                    live=start.live, embedded=start.embedded + written
                ),
                fraction=fraction,
            )
            token.checkpoint()
            if after is None:
                break
    analyze_memory(conn)


def _report(
    uow: HandlerUnitOfWork,
    payload: EmbeddingRebuildPayload,
    provider: EmbeddingProvider,
    *,
    coverage: EmbeddingCoverage,
    fraction: float,
) -> None:
    """Write progress onto the operation record the payload names.

    The text carries this workspace's own coverage for the model — how many of its live
    memories have a vector — which is where that figure is surfaced.
    """
    recorded = set_progress(
        uow.connection,
        operation_id=payload.operation_id,
        progress_text=(
            f"{coverage.embedded} of {coverage.live} live memories embedded "
            f"with {provider.model_id}"
        ),
        progress_fraction=fraction,
    )
    if not recorded:
        raise ProgressUnrecorded(
            f"operation {payload.operation_id} is not an open record to report on"
        )
