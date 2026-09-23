"""The embed job: one memory's vector, written after the memory's own commit.

**Three outcomes, and only one of them is a failure.**

- *Nothing to do* — the memory is gone, invalidated or superseded; no provider resolves
  any more (the configuration changed under a queued row); or the memory already has a
  row for the provider's model (a rebuild got there first). Each completes successfully
  having written nothing. A missing provider is not an operator's mistake to put in the
  failure list, and the next rebuild picks the memory up.
- *Embedded* — one row, written through the dense strategy's single writer.
- *The provider raised* — the exception propagates, the core's ordinary retry and
  backoff apply, and a provider that keeps failing lands in the core failure list once
  ``EMBED_MAX_ATTEMPTS`` is spent.

**No context anywhere in here.** A job handler holds none and no boundary factory can
mint one for it, which is why the whole write side takes a unit of work and a resolved
provider instead.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.work.cancellation import CancellationToken

from rheo_recallatron.embedding.registry import resolve_provider
from rheo_recallatron.retrieval.dense import DenseStrategy
from rheo_recallatron.retrieval.protocol import IndexItem
from rheo_recallatron.storage.repository import (
    get_memory,
    get_memory_embedding,
    is_live_memory,
)


class EmbedJobPayload(BaseModel):
    """What the enqueue helper writes: the memory, and nothing else.

    ``extra = "forbid"`` because the helper writes exactly ``{"memory_id": ...}``, and a
    payload carrying more did not come from it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_id: UUID


def run_embed_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """Embed one live memory with the provider configured *now*, or do nothing."""
    if not isinstance(payload, EmbedJobPayload):
        raise TypeError(f"expected an EmbedJobPayload, got {type(payload).__name__}")
    token.checkpoint()
    row = get_memory(uow.connection, payload.memory_id)
    if row is None or not is_live_memory(row):
        return
    provider = resolve_provider()
    if provider is None:
        return
    if get_memory_embedding(uow.connection, row.id, provider.model_id) is not None:
        return
    DenseStrategy().index_many(
        uow, [IndexItem(row.id, row.title, row.body)], provider=provider
    )
