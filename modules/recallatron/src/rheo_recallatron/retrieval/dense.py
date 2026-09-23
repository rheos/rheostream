"""``DenseStrategy``, write side: the one writer of ``memory_embedding`` rows.

**The write side takes no context.** :meth:`DenseStrategy.index_many` takes a unit of
work and a resolved provider and nothing else, because its callers are worker jobs: a
job handler is ``(HandlerUnitOfWork, payload, CancellationToken)``, holds no
``WorkspaceContext``, and no boundary factory can mint one for it. ``index`` and
``invalidate`` carry ``ctx`` only because the ``RetrievalStrategy`` protocol does.

Not registered in the strategy registry yet, and it cannot be: registering it is the
same change that offers ``dense`` in the strategy key's ``choices``, and the registry's
import-time check refuses the other order.

**The lifecycle closure does not call** :meth:`DenseStrategy.invalidate`. Correction,
supersession and deletion delete a memory's vectors through the repository directly and
unconditionally, whichever strategy is configured: a workspace that moved from ``dense``
back to ``lexical`` must still drop the vectors its dense period wrote.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from rheo_contracts import WorkspaceContext
from rheo_core.refs.resolver import UnitOfWork

from rheo_recallatron.configuration import STRATEGY_DENSE
from rheo_recallatron.embedding import embed_input
from rheo_recallatron.embedding.protocol import EmbeddingProvider
from rheo_recallatron.embedding.registry import resolve_provider
from rheo_recallatron.references import canonical_ref
from rheo_recallatron.retrieval.protocol import IndexItem
from rheo_recallatron.storage.repository import (
    MemoryEmbeddingRow,
    delete_memory_embeddings,
    insert_memory_embedding,
)


class DenseStrategy:
    """Ranks by cosine similarity to the embedded query (the read side is to come)."""

    name: Final = STRATEGY_DENSE

    def index_many(
        self,
        uow: UnitOfWork,
        items: Sequence[IndexItem],
        *,
        provider: EmbeddingProvider,
    ) -> int:
        """Embed every item in one provider call, write one row each, answer how many.

        Each item embeds :func:`~rheo_recallatron.embedding.embed_input` of its title
        and body, under the provider's ``model_id`` and width. There is no clock in the
        signature, so the rows are stamped here, as a handler stamps its own writes. A
        provider that raises, or answers the wrong number of vectors, raises out of this
        call before a single row is written.
        """
        if not items:
            return 0
        vectors = provider.embed([embed_input(item.title, item.body) for item in items])
        if len(vectors) != len(items):
            raise ValueError(
                f"embedding provider {provider.model_id!r} answered {len(vectors)} "
                f"vectors for {len(items)} texts"
            )
        embedded_at = datetime.now(UTC)
        for item, vector in zip(items, vectors, strict=True):
            insert_memory_embedding(
                uow.connection,
                MemoryEmbeddingRow(
                    memory_id=item.memory_id,
                    model_id=provider.model_id,
                    dimensions=provider.dimensions,
                    vector=vector,
                    embedded_at=embedded_at,
                ),
            )
        return len(items)

    def index(self, ctx: WorkspaceContext, uow: UnitOfWork, item: IndexItem) -> None:
        """Embed one memory with the configured provider; nothing when none resolves."""
        provider = resolve_provider()
        if provider is None:
            return
        self.index_many(uow, [item], provider=provider)

    def invalidate(self, ctx: WorkspaceContext, uow: UnitOfWork, ref: str) -> None:
        """Remove every vector of the memory ``ref`` names, whatever model wrote it."""
        delete_memory_embeddings(uow.connection, canonical_ref(ref).id)
